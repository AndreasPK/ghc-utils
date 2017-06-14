import gdb
import gdb.printing

bits = 64
profiled = False
tntc = True

StgClosure = gdb.lookup_type('StgClosure')
StgClosurePtr = gdb.lookup_type('StgClosure').pointer()
StgInfoTable = gdb.lookup_type('StgInfoTable')
StgConInfoTable = gdb.lookup_type('StgConInfoTable')
StgRetInfoTable = gdb.lookup_type('StgRetInfoTable')
StgFunInfoTable = gdb.lookup_type('StgFunInfoTable')
StgClosureInfo = gdb.lookup_type('StgClosureInfo')
StgWord = gdb.lookup_type('uintptr_t')
StgPtr = gdb.lookup_type('void').pointer()

if bits == 32:
    BITMAP_SIZE_MASK = 0x1f
    BITMAP_SIZE_SHIFT = 5
else:
    BITMAP_SIZE_MASK = 0x3f
    BITMAP_SIZE_SHIFT = 6

class ClosureType(object):
    INVALID_OBJECT                = 0
    CONSTR                        = 1
    CONSTR_1_0                    = 2
    CONSTR_0_1                    = 3
    CONSTR_2_0                    = 4
    CONSTR_1_1                    = 5
    CONSTR_0_2                    = 6
    CONSTR_NOCAF                  = 7
    FUN                           = 8
    FUN_1_0                       = 9
    FUN_0_1                       = 10
    FUN_2_0                       = 11
    FUN_1_1                       = 12
    FUN_0_2                       = 13
    FUN_STATIC                    = 14
    THUNK                         = 15
    THUNK_1_0                     = 16
    THUNK_0_1                     = 17
    THUNK_2_0                     = 18
    THUNK_1_1                     = 19
    THUNK_0_2                     = 20
    THUNK_STATIC                  = 21
    THUNK_SELECTOR                = 22
    BCO                           = 23
    AP                            = 24
    PAP                           = 25
    AP_STACK                      = 26
    IND                           = 27
    IND_STATIC                    = 28
    RET_BCO                       = 29
    RET_SMALL                     = 30
    RET_BIG                       = 31
    RET_FUN                       = 32
    UPDATE_FRAME                  = 33
    CATCH_FRAME                   = 34
    UNDERFLOW_FRAME               = 35
    STOP_FRAME                    = 36
    BLOCKING_QUEUE                = 37
    BLACKHOLE                     = 38
    MVAR_CLEAN                    = 39
    MVAR_DIRTY                    = 40
    TVAR                          = 41
    ARR_WORDS                     = 42
    MUT_ARR_PTRS_CLEAN            = 43
    MUT_ARR_PTRS_DIRTY            = 44
    MUT_ARR_PTRS_FROZEN0          = 45
    MUT_ARR_PTRS_FROZEN           = 46
    MUT_VAR_CLEAN                 = 47
    MUT_VAR_DIRTY                 = 48
    WEAK                          = 49
    PRIM                          = 50
    MUT_PRIM                      = 51
    TSO                           = 52
    STACK                         = 53
    TREC_CHUNK                    = 54
    ATOMICALLY_FRAME              = 55
    CATCH_RETRY_FRAME             = 56
    CATCH_STM_FRAME               = 57
    WHITEHOLE                     = 58
    SMALL_MUT_ARR_PTRS_CLEAN      = 59
    SMALL_MUT_ARR_PTRS_DIRTY      = 60
    SMALL_MUT_ARR_PTRS_FROZEN0    = 61
    SMALL_MUT_ARR_PTRS_FROZEN     = 62
    COMPACT_NFDATA                = 63

def build_closure_printers():
    C = ClosureType
    p = {}
    for ty, v in ClosureType.__dict__.items():
        if isinstance(v, int):
            # damn you python and your terrible scoping
            def get_printer(ty):
                return lambda closure: str(ty)
            p[v] = get_printer(ty)

    def invalid_object(closure):
        raise RuntimeError('invalid object')
    p[C.INVALID_OBJECT] = invalid_object

    def constr(closure):
        con_info = get_con_itbl(closure)
        s = 'constr' % con_info
        # TODO
        return s
    for ty in [C.CONSTR,
               C.CONSTR_1_0, C.CONSTR_0_1,
               C.CONSTR_1_1, C.CONSTR_0_2, C.CONSTR_2_0,
               C.CONSTR_NOCAF]:
        p[ty] = constr

    def fun(closure):
        return 'FUN'
    for ty in [C.FUN, C.FUN_1_0, C.FUN_0_1, C.FUN_1_1,
               C.FUN_0_2, C.FUN_2_0, C.FUN_STATIC]:
        p[ty] = fun

    def thunk(closure):
        return 'THUNK'
    for ty in [C.THUNK, C.THUNK_1_0, C.THUNK_0_1, C.THUNK_1_1,
               C.THUNK_0_2, C.THUNK_2_0, C.THUNK_STATIC]:
        p[ty] = thunk

    def thunk_sel(closure):
        ty = gdb.lookup_type('StgSelector').pointer()
        selectee = closure.cast(ty)['selectee']
        return 'THUNK_SELECTOR(%s, %s)' % (selectee, print_closure(selectee))
    p[C.THUNK_SELECTOR] = thunk_sel

    def application(ty, closure):
        ap_ = closure.cast(ty).dereference()
        things = [ap_['fun']]
        for i in ap['n_args']:
            things.append((ap_['payload'] + i).dereference())
        return 'AP(%s)' % ', '.join(things)
    p[C.AP] = lambda closure: application(gdb.lookup_type('StgAP').pointer(), closure)
    p[C.PAP] = lambda closure: application(gdb.lookup_type('StgPAP').pointer(), closure)

    def ap_stack(closure):
        ty = gdb.lookup_type('StgAP_STACK').pointer()
        ap_ = closure.cast(ty).dereference()
        return 'AP_STACK(size=%s, fun=%s, payload=%s)' % \
            (ap_['size'], print_closure(ap_['fun']), ap_['payload'])
    p[C.AP_STACK] = ap_stack

    def update_frame(closure):
        ty = gdb.lookup_type('StgUpdateFrame').pointer()
        updatee = closure.cast(ty)['updatee']
        return 'UPDATE_FRAME(%s: %s)' % (updatee, print_closure(updatee))
    p[C.UPDATE_FRAME] = update_frame

    def indirect(closure):
        ty = gdb.lookup_type('StgInd').pointer()
        ind = closure.cast(ty)['indirectee']
        return 'BLACKHOLE(%s: %s)' % (ind, print_closure(ind))
    p[C.IND] = indirect
    p[C.IND_STATIC] = indirect
    p[C.BLACKHOLE] = indirect

    return p

closurePrinters = build_closure_printers()


class InfoTablePrinter(object):
    def __init__(self, val):
        self.val = val

    def to_string(self):
        v = self.val
        s = ""
        #s += '(ptrs=%08x  nptrs=%08x)' % (v['payload']['ptrs'], v['payload']['nptrs'])
        return s

    def display_hint(self):
        return 'info table'

def get_itbl(closure):
    assert closure.type == StgClosurePtr
    info_ptr = closure.dereference()['header']['info']
    return info_ptr.cast(StgInfoTable.pointer()) - 1
    #return (gdb.parse_and_eval('get_itbl(%s)' % closure))

def get_con_itbl(closure):
    assert closure.type == StgClosurePtr
    info_ptr = closure.dereference()['header']['info']
    return info_ptr.cast(StgConInfoTable.pointer()) - 1
    #return (gdb.parse_and_eval('get_con_itbl(%s)' % closure))

def get_ret_itbl(closure):
    assert closure.type == StgClosurePtr
    info_ptr = closure.dereference()['header']['info']
    return info_ptr.cast(StgRetInfoTable.pointer()) - 1

def get_fun_itbl(closure):
    assert closure.type == StgClosurePtr
    info_ptr = closure.dereference()['header']['info']
    return info_ptr.cast(StgFunInfoTable.pointer()) - 1

def print_closure(closure):
    assert closure.type == StgClosurePtr
    closure = untag(closure)
    info = get_itbl(closure)
    ty = int(info['type'])
    return closurePrinters[ty](closure)

def iter_small_bitmap(bitmap):
    size = bitmap & BITMAP_SIZE_MASK
    bits = bitmap >> BITMAP_SIZE_SHIFT
    for i in range(size):
        isWord = bitmap & (1 << i) != 0
        yield isWord

class PrintInfoCmd(gdb.Command):
    def __init__(self):
        super(PrintInfoCmd, self).__init__ ("print_info", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        print(get_itbl(gdb.parse_and_eval(arg)))

class PrintGhcStackCmd(gdb.Command):
    def __init__(self):
        super(PrintGhcStackCmd, self).__init__ ("ghc-backtrace", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        sp = gdb.parse_and_eval('$rbp').cast(StgPtr)
        maxDepth = 10
        if arg:
            maxDepth = int(arg)
        showSpAddrs = False
        for i in range(maxDepth):
            print('%d: ' % i, end='')
            #info = (sp.cast(StgInfoTable.pointer().pointer()).dereference() - 1).dereference()
            #info = get_info_table(sp.cast(StgClosurePtr.pointer()).dereference())
            info = get_itbl(sp.cast(StgClosurePtr))
            ty = info['type']
            if ty in [ ClosureType.UPDATE_FRAME,
                       ClosureType.CATCH_FRAME ]:
                print(print_closure(sp.cast(StgClosurePtr)))

            elif ty == ClosureType.UNDERFLOW_FRAME:
                print(print_closure(sp.cast(StgClosurePtr)))
                break

            elif ty == ClosureType.STOP_FRAME:
                print(print_closure(sp.cast(StgClosurePtr)))
                break

            elif ty == ClosureType.RET_SMALL:
                c = sp.cast(StgWord.pointer().pointer()).dereference()
                bitmap = info['layout']['bitmap']
                print('RET_SMALL')
                print('  return = %s (%s)' % (c, gdb.find_pc_line(int(c.cast(StgWord)))))
                for i, isWord in enumerate(iter_small_bitmap(bitmap)):
                    w = sp.cast(StgWord.pointer()) + i
                    if showSpAddrs:
                        print('  field %d (%x): ' % (i, w.cast(StgWord)), end='')
                    else:
                        print('  field %d: ' % i, end='')
                    if isWord:
                        print('Word %d' % (w.dereference()))
                    else:
                        print('Ptr  %s' % (w.dereference().cast(StgPtr)))

            elif ty == ClosureType.RET_BCO:
                print('RET_BCO')
                raise NotImplemented

            elif ty == ClosureType.RET_FUN:
                print('RET_FUN')
                raise NotImplemented
            else:
                raise RuntimeError('unknown stack frame type %d' % ty)

            size = stack_frame_size(sp.cast(StgClosurePtr))
            sp = sp + StgPtr.sizeof * size

def untag(ptr):
    assert ptr.type == StgClosurePtr
    return (ptr.cast(StgWord) & ~7).cast(StgClosurePtr)

def stack_frame_size(frame):
    assert frame.type == StgClosurePtr
    #return gdb.parse_and_eval('stack_frame_sizeW(0x%x)' % sp)
    info = get_ret_itbl(frame)
    ty = info.dereference()['i']['type']
    if ty == ClosureType.RET_FUN:
        size = frame.cast(StgRetFun.pointer()).dereference()['size']
        return gdb.parse_and_eval('sizeof(StgRetFun)') + size
    elif ty == ClosureType.RET_BIG:
        raise NotImplemented
    elif ty == ClosureType.RET_BCO:
        raise NotImplemented
    else:
        bitmap = info.dereference()['i']['layout']['bitmap']
        size = bitmap & BITMAP_SIZE_MASK
        return 1 + size

def build_pretty_printer():
    pp = gdb.printing.RegexpCollectionPrettyPrinter("ghc")
    #pp.add_printer('StgInfoTable', '^StgInfoTable$', InfoTablePrinter)
    return pp

PrintGhcStackCmd()
PrintInfoCmd()
gdb.printing.register_pretty_printer(gdb.current_objfile(), build_pretty_printer(), replace=True)