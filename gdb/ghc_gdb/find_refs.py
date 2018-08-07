import typing
from typing import List, Optional, Tuple, TypeVar, Callable, NamedTuple, Set, Dict, Iterator, Any
from . import ghc_heap
from .types import *
from . import closure
from .utils import CommandWithArgs
import gdb

T = TypeVar('T')

# This is a terrible hack but it's better than searching through 1TB of address space.
# Unfortunately while gdb's `find` command seems to be smart enough to only search
# mapped memory, the same can't be said of Python's search_memory.
heap_start = Ptr(0x4200000000)
heap_end = Ptr(0x4210000000)

def search_memory_many(inferior: gdb.Inferior, start: Ptr, end: Ptr, val: bytes) -> Iterator[Ptr]:
    #print('Searching for %s' % val)
    while True:
        addr = inferior.search_memory(start.addr, end.addr - start.addr, val)
        if addr is None:
            break
        else:
            addr = Ptr(addr)
            yield addr
            start = addr.offset_bytes(1)

def find_symbol(addr: Ptr) -> Optional[gdb.Symbol]:
    """ Find the top-level Symbol associated with an address. """
    try:
        block = gdb.block_for_pc(addr.addr)
    except RuntimeError:
        return None

    if block is None:
        return None

    while block and not block.function:
        block = block.superblock

    return block.function if block is not None else None

def find_refs(closure_ptr: Ptr, include_static=True) -> Iterator[Ptr]:
    """ Find all references to a closure. """
    inf = gdb.selected_inferior()
    closure_ptr = closure_ptr.untagged()
    for tag in range(0,7):
        val = closure_ptr.tagged(tag).pack() # type: bytes

        if include_static:
            yield from search_memory_many(inf, Ptr(0), Ptr(0x10000000), val)
        yield from search_memory_many(inf, heap_start, heap_end, val) # heap

RecRefs = List[Tuple[Ptr, 'RecRefs']]

class Tree(typing.Generic[T]):
    def __init__(self, node: T, children: List['Tree[T]']) -> None:
        self.node = node
        self.children = children

    def _render(self, render_node: Callable[[T], str], indent: int) -> str:
        indentation = ' ' * (2*indent)
        return '\n'.join([indentation + render_node(self.node)] +
                         [child._render(render_node, indent+1)
                          for child in self.children])

    def render(self, render_node: Callable[[T], str]) -> str:
        return self._render(render_node, 0)

    def nodes(self) -> Set[T]:
        """ List all nodes """
        nodes = { a for (a,_) in self.edges() }
        nodes |= { a for (_,a) in self.edges() }
        return nodes

    def depth(self) -> int:
        depth = 0
        for c in self.children:
            depth = max(c.depth(), depth)

        return depth + 1

    def edges(self) -> List[Tuple[T, T]]:
        return [ (self.node, child.node)
                 for child in self.children ] + \
               [ edge
                 for child in self.children
                 for edge in child.edges() ]

    def to_dot(self, node_name: Callable[[T], str], node_attrs: Callable[[T], Dict[str, str]] = lambda _: {}) -> str:
        def format_attrs(attrs):
            return ', '.join('"%s"="%s"' % (name, val) for name, val in attrs.items())

        lines = ['digraph {']
        lines += ['  "%s" -> "%s" [dir=back];' % (node_name(a), node_name(b))
                  for (a,b) in self.edges()]
        lines += ['  "%s" [%s];' % (node_name(n), format_attrs(node_attrs(n)))
                  for n in self.nodes()]
        lines += ['}']
        return '\n'.join(lines)


class ClosureRef(NamedTuple):
    referring_field: Ptr
    referring_closure: Optional[Ptr]

def find_refs_rec(closure_ptr: Ptr, depth: int, include_static = True) -> Tree[ClosureRef]:
    """
    Recursively search for references to a closure up to the given depth.
    """
    inf = gdb.selected_inferior()

    def go(closure_ptr: Ptr, seen_closures: Set[Ptr], depth: int) -> List[Tree[ClosureRef]]:
        if depth == 0 or closure_ptr in seen_closures:
            return []
        else:
            refs = [] # type: List[Tree[ClosureRef]]
            for ref in find_refs(closure_ptr, include_static=include_static):
                ref_start = find_containing_closure(inf, ref)
                rec_refs = []
                if ref_start is not None:
                    rec_refs = go(ref_start, seen_closures | {closure_ptr}, depth-1)
                else:
                    print('Failed to find beginning of %s' % ref)

                refs += [Tree(ClosureRef(ref, ref_start), rec_refs)]

            return refs

    # Root ClosureRef is a bit of a hack
    return Tree(ClosureRef(closure_ptr, closure_ptr),
                go(closure_ptr, set(), depth))

def find_containing_closure(inferior: gdb.Inferior, ptr: Ptr) -> Optional[Ptr]:
    """
    Try to identify the beginning of the closure containing the given address.
    Note that this is quite heuristic (looking for something that looks like an
    info table)
    """
    max_closure_size = 8*4096
    ptr = ptr.untagged()
    for i in range(max_closure_size // word_size):
        start = ptr.offset_bytes(- i * word_size)
        bs = inferior.read_memory(start.addr, word_size)
        addr = Ptr.unpack(bs)
        sym = find_symbol(addr)
        # Is this an info table pointer?
        if sym is not None and sym.print_name.endswith('_info'): # and sym.value == addr:
            info = ghc_heap.get_itbl(gdb.parse_and_eval('(StgClosure *) %d' % start.addr))
            nptrs = int(info['layout']['payload']['ptrs'])
            if i <= nptrs + 5: # A bit of fudge for the headers
                return start
            else:
                print('suspicious info table: too far (field=%s, info@%s=%s, nptrs=%d, i=%d)' % (ptr, start, sym.print_name, nptrs, i))
                return None

    return None

closureTypeDict = { v: str(ty) for ty, v in closure.ClosureType.__dict__.items() }

def get_bdescr(ptr: Ptr) -> Optional[Any]:
    if ptr < heap_start: return None # XXX

    if True or gdb.parse_and_eval('HEAP_ALLOCED(%d)' % ptr.addr):
        return gdb.parse_and_eval('Bdescr(%d)' % ptr.addr).dereference()
    else:
        return None

BF_NONMOVING = 1024 # gdb.parse_and_eval('BF_NONMOVING')
NONMOVING_SEGMENT_MASK = (1 << 15) - 1

def get_nonmoving_segment(ptr: Ptr) -> Optional[Tuple[gdb.Value, int]]:
    bd = get_bdescr(ptr)
    if bd is not None and bd['flags'] & BF_NONMOVING:
        seg_base = ptr.addr & ~NONMOVING_SEGMENT_MASK
        block_idx = int(gdb.parse_and_eval('nonmoving_get_block_idx(%s)' % ptr))
        seg = gdb.parse_and_eval('(struct nonmoving_segment *) %s' % seg_base).dereference()
        return (seg, block_idx)
    else:
        return None

def refs_dot(graph: Tree[ClosureRef]) -> str:
    node_name = lambda ptr: str(ptr.referring_field)
    def node_attrs(ref: ClosureRef):
        try:
            if ref.referring_closure is not None:
                itbl = ghc_heap.get_itbl(gdb.parse_and_eval('(StgClosure *) %d' % (ref.referring_closure.addr,))).dereference()
                closure_type = closureTypeDict.get(int(itbl['type']), 'unknown')
            else:
                closure_type = 'invalid'
        except gdb.MemoryError:
            closure_type = 'invalid itbl'

        extra = ''
        bd = get_bdescr(ref.referring_field)
        if bd is not None and bd['flags'] & BF_NONMOVING:
            seg, blk = get_nonmoving_segment(ref.referring_field)
            mark = seg['bitmap'][blk]
            if mark != 0:
                color = 'blue'
                extra += '\nepoch=%d' % mark
            else:
                color = 'red'
                extra += '\nunmarked'
        else:
            color = 'black'

        # ! means that we couldn't find the start of the containing closure;
        # pointer identifies field
        label = '\n'.join([str(ref.referring_closure)
                           if ref.referring_closure is not None
                           else '! %s' % ref.referring_field,
                           '%s%s' % (closure_type, extra)])
        return {'label': label,
                'fontcolor': color}

    return graph.to_dot(node_name, node_attrs=node_attrs)

class ExportClosureDepsDot(CommandWithArgs):
    """ Print a chain of block descriptors """
    command_name = "ghc closure-deps"

    def build_parser(self, parser):
        parser.add_argument('-d', '--depth', default=5, type=int, help='Maximum search depth')
        parser.add_argument('-o', '--output', default='deps.dot',
                            metavar='FILE', type=str, help='Output dot file path')
        parser.add_argument('-n', '--no-static', action='store_true', help="Don't search static maps")
        parser.add_argument('closure_ptr', type=str, help='A pointer to a closure')

    def run(self, opts, from_tty):
        closure_ptr = Ptr(gdb.parse_and_eval(opts.closure_ptr))
        graph = find_refs_rec(closure_ptr, depth=opts.depth,
                              include_static=not opts.no_static)
        with open(opts.output, 'w') as f:
            f.write(refs_dot(graph))

        print('Found %d referring closures to depth %d' %
              (len(graph.nodes()), graph.depth()))
        print('Written to %s' % opts.output)

ExportClosureDepsDot()
