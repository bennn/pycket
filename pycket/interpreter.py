from pycket                   import values
from pycket                   import vector
from pycket.prims             import prim_env
from pycket.error             import SchemeException
from pycket.cont              import Cont
from rpython.rlib             import jit, debug, objectmodel
from rpython.rlib.objectmodel import r_dict, compute_hash
from small_list               import inline_small_list


class GlobalConfig(object):
    config = {}
    loaded = False

    @staticmethod
    def lookup(s):
        return GlobalConfig.instance.config.get(s, None)

    @staticmethod
    def load(ast):
        if GlobalConfig.instance.loaded: return
        GlobalConfig.instance.loaded = True
        assert isinstance(ast, Module)
        GlobalConfig.instance.config.update(ast.config)
GlobalConfig.instance = GlobalConfig()

def variable_set():
    " new set-like structure for variables "
    def var_eq(a, b):
        if isinstance(a, LexicalVar) and isinstance(b, LexicalVar):
            return a.sym is b.sym
        elif isinstance(a, ModuleVar) and isinstance(b, ModuleVar):
            # two renamed variables can be the same
            return (a.srcmod == b.srcmod and a.srcsym is b.srcsym)
        return False
    def var_hash(a):
        if isinstance(a, LexicalVar):
            return compute_hash(a.sym)
        elif isinstance(a, ModuleVar):
            return compute_hash( (a.srcsym, a.srcmod) )
        assert False
    return r_dict(var_eq, var_hash, force_non_null=True)

def variables_equal(a, b):
    if len(a) != len(b):
        return False
    for k, v in a.iteritems():
         if not k in b:
             return False
    return True

def variable_name(v):
    return v.value

class ModuleEnv(object):
    _immutable_fields_ = ["modules"]
    def __init__(self):
        self.modules = {}
        self.current_module = None

    def require(self, module_name):
        assert 0
        # load the file, evaluate it, register it in the table

    def add_module(self, name, module):
        # note that `name` and `module.name` are different!
        assert isinstance(module, Module)
        self.modules[name] = module

    @jit.elidable
    def _find_module(self, name):
        return self.modules.get(name, None)

class Env(object):
    _immutable_fields_ = ["toplevel_env", "module_env"]
    _attrs_ = ['toplevel_env']

class Version(object):
    pass

class ToplevelEnv(Env):
    _immutable_fields_ = ["version?", "module_env", "toplevel_env"]
    def __init__(self):
        self.bindings = {}
        self.version = Version()
        self.toplevel_env = self # bit silly
        self.module_env = ModuleEnv()
        self.commandline_arguments = []

    def lookup(self, sym, env_structure):
        raise SchemeException("variable %s is unbound" % variable_name(sym))

    def toplevel_lookup(self, sym):
        jit.promote(self)
        w_res = self._lookup(sym, jit.promote(self.version))
        if isinstance(w_res, values.W_Cell):
            w_res = w_res.get_val()
        return w_res

    @jit.elidable
    def _lookup(self, sym, version):
        try:
            return self.bindings[sym]
        except KeyError:
            raise SchemeException("toplevel variable %s not found" % variable_name(sym))

    def toplevel_set(self, sym, w_val):
        if sym in self.bindings:
            self.bindings[sym].set_val(w_val)
        else:
            self.bindings[sym] = values.W_Cell(w_val)
            self.version = Version()

class ConsEnv(Env):
    _immutable_fields_ = ["prev", "toplevel_env"]
    def __init__ (self, prev, toplevel):
        self.toplevel_env = toplevel
        self.prev = prev

    @jit.unroll_safe
    def lookup(self, sym, env_structure):
        jit.promote(env_structure)
        for i, s in enumerate(env_structure.elems):
            if s is sym:
                v = self._get_list(i)
                assert v is not None
                return v
        return self.prev.lookup(sym, env_structure.prev)

    @jit.unroll_safe
    def set(self, sym, val, env_structure):
        jit.promote(env_structure)
        for i, s in enumerate(env_structure.elems):
            if s is sym:
                self._set_list(i, val)
                return
        return self.prev.set(sym, val, env_structure.prev)
inline_small_list(ConsEnv, immutable=True, attrname="vals")

def check_one_val(vals):
    if vals._get_size_list() != 1:
        raise SchemeException("expected 1 value but got %s"%(vals._get_size_list()))
    w_val = vals._get_list(0)
    return w_val

class LetrecCont(Cont):
    _immutable_fields_ = ["ast", "env", "prev", "i"]
    def __init__(self, ast, i, env, prev):
        Cont.__init__(self, env, prev)
        self.ast = ast
        self.i = i

    @jit.unroll_safe
    def plug_reduce(self, _vals):
        vals = _vals._get_full_list()
        ast = jit.promote(self.ast)
        if ast.counts[self.i] != _vals._get_size_list():
            raise SchemeException("wrong number of values")
        for j, w_val in enumerate(vals):
            v = self.env.lookup(ast.args.elems[ast.total_counts[self.i] + j], ast.args)
            assert isinstance(v, values.W_Cell)
            v.set_val(w_val)
        if self.i >= (len(ast.rhss) - 1):
            return ast.make_begin_cont(self.env, self.prev)
        else:
            return (ast.rhss[self.i + 1], self.env,
                    LetrecCont(ast, self.i + 1,
                               self.env, self.prev))

class LetCont(Cont):
    _immutable_fields_ = ["ast", "env", "prev"]

    def __init__(self, ast, rhsindex, env, prev):
        Cont.__init__(self, env, prev)
        self.ast  = ast
        self.rhsindex = rhsindex

    def plug_reduce(self, _vals):
        vals = _vals._get_full_list()
        ast = jit.promote(self.ast)
        rhsindex = jit.promote(self.rhsindex)
        if ast.counts[rhsindex] != len(vals):
            raise SchemeException("wrong number of values")
        if rhsindex == (len(ast.rhss) - 1):
            vals_w = self._get_full_list() + vals
            env = ConsEnv.make(vals_w, self.env, self.env.toplevel_env)
            return ast.make_begin_cont(env, self.prev)
        else:
            return (ast.rhss[rhsindex + 1], self.env,
                    LetCont.make(self._get_full_list() + vals, ast,
                                 rhsindex + 1, self.env, self.prev))

inline_small_list(LetCont, attrname="vals_w", immutable=True)


class CellCont(Cont):
    _immutable_fields_ = ["env", "prev"]

    def __init__(self, ast, env, prev):
        Cont.__init__(self, env, prev)
        self.ast = ast

    @jit.unroll_safe
    def plug_reduce(self, vals):
        ast = jit.promote(self.ast)
        vals_w = []
        for i, needs_cell in enumerate(ast.need_cell_flags):
            w_val = vals._get_list(i)
            if needs_cell:
                w_val = values.W_Cell(w_val)
            vals_w.append(w_val)
        return return_multi_vals(values.Values.make(vals_w), self.env, self.prev)

class SetBangCont(Cont):
    _immutable_fields_ = ["ast", "env", "prev"]
    def __init__(self, ast, env, prev):
        Cont.__init__(self, env, prev)
        self.ast = ast
    def plug_reduce(self, vals):
        w_val = check_one_val(vals)
        self.ast.var._set(w_val, self.env)
        return return_value(values.w_void, self.env, self.prev)

class BeginCont(Cont):
    _immutable_fields_ = ["ast", "env", "prev", "i"]
    def __init__(self, ast, i, env, prev):
        Cont.__init__(self, env, prev)
        self.ast = ast
        self.i = i

    def plug_reduce(self, vals):
        return jit.promote(self.ast).make_begin_cont(self.env, self.prev, self.i)

# FIXME: it would be nice to not need two continuation types here
class Begin0Cont(Cont):
    _immutable_fields_ = ["ast", "env", "prev"]
    def __init__(self, ast, env, prev):
        Cont.__init__(self, env, prev)
        self.ast = ast
    def plug_reduce(self, vals):
        return self.ast.body, self.env, Begin0FinishCont(self.ast, vals, self.env, self.prev)

class Begin0FinishCont(Cont):
    _immutable_fields_ = ["ast", "vals", "env", "prev"]
    def __init__(self, ast, vals, env, prev):
        Cont.__init__(self, env, prev)
        self.ast = ast
        self.vals = vals
    def plug_reduce(self, vals):
        return return_multi_vals(self.vals, self.env, self.prev)

class WCMKeyCont(Cont):
    _immutable_fields_ = ["ast", "env", "prev"]
    def __init__(self, ast, env, prev):
        Cont.__init__(self, env, prev)
        self.ast = ast
    def plug_reduce(self, vals):
        key = check_one_val(vals)
        return self.ast.value, self.env, WCMValCont(self.ast, key, self.env, self.prev)

class WCMValCont(Cont):
    _immutable_fields_ = ["ast", "env", "prev", "key"]
    def __init__(self, ast, key, env, prev):
        Cont.__init__(self, env, prev)
        self.ast = ast
        self.key = key
    def plug_reduce(self, vals):
        val = check_one_val(vals)
        # FIXME: can prev be null?
        assert self.prev
        self.prev.update_cm(self.key, val)
        return self.ast.body, self.env, self.prev


class Done(Exception):
    def __init__(self, vals):
        self.values = vals

class AST(object):
    _attrs_ = ["should_enter"]
    _immutable_fields_ = ["should_enter?"]
    _settled_ = True

    should_enter = False # default value

    simple = False

    def defined_vars(self): return {}

    def interpret(self, env, cont):
        # default implementation for simple AST forms
        assert self.simple
        return return_value(self.interpret_simple(env), env, cont)
    def interpret_simple(self, env):
        raise NotImplementedError("abstract base class")

    def free_vars(self):
        return {}
    def assign_convert(self, vars, env_structure):
        """ make a copy of the AST that converts all writable variables into
        using cells. In addition, compute the state of the environment for
        every AST node that needs to know.

        The vars argument contains the variables that need to use cells.
        The env_structure is an instance of SymList (or None) describing the
        environment at that AST node.
        """
        raise NotImplementedError("abstract base class")
    def mutated_vars(self):
        raise NotImplementedError("abstract base class")
    def tostring(self):
        return "UNKNOWN AST: "

class Module(AST):
    _immutable_fields_ = ["name", "body"]
    def __init__(self, name, body, config):
        self.name = name
        self.body = body
        self.env = None
        self.config = config
        defs = {}
        for b in body:
            defs.update(b.defined_vars())
        self.defs = defs

    @jit.elidable
    def lookup(self, sym):
        assert (sym in self.defs)
        v = self.defs[sym]
        if not v:
            raise SchemeException("use of module variable before definition %s" % (sym))
        return v

    # these are both empty and irrelevant for modules
    def mutated_vars(self): return variable_set()
    def free_vars(self): return {}

    # all the module-bound variables that are mutated
    def _mutated_vars(self):
        x = variable_set()
        for r in self.body:
            x.update(r.mutated_vars())
        return x

    def assign_convert(self, vars, env_structure):
        local_muts = self._mutated_vars()
        new_vars = vars.copy()
        for k, v in local_muts.iteritems():
            new_vars[k] = v
        new_body = [b.assign_convert(new_vars, env_structure) for b in self.body]
        return Module(self.name, new_body, self.config)
    def tostring(self):
        return "(module %s %s)"%(self.name," ".join([s.tostring() for s in self.body]))

    def interpret_mod(self, env):
        self.env = env
        old = env.module_env.current_module
        env.module_env.current_module = self
        for f in self.body:
            # FIXME: this is wrong -- the continuation barrier here is around the RHS,
            # whereas in Racket it's around the whole `define-values`
            if isinstance(f, DefineValues):
                e = f.rhs
                vs = interpret_one(e, self.env)._get_full_list()
                if len(f.names) == len(vs):
                    for n in range(len(vs)):
                        self.defs[f.names[n]] = vs[n]
                else:
                    raise SchemeException("wrong number of values for define-values")
            else: # FIXME modules can have other things, assuming expression
                vs = interpret_one(f, self.env)
                continue
        env.module_env.current_module = old

class Require(AST):
    _immutable_fields_ = ["modname", "module"]
    simple = True

    def __init__(self, modname, module):
        self.modname = modname
        self.module  = module

    def mutated_vars(self):
        return variable_set()
    def free_vars(self):
        return {}

    def assign_convert(self, vars, env_structure):
        return self

    # Interpret the module and add it to the module environment
    def interpret_simple(self, env):
        top = env.toplevel_env
        top.module_env.add_module(self.modname, self.module)
        mod = self.module.interpret_mod(top)
        return values.w_void
    def tostring(self):
        return "(require %s)"%self.modname

    def tostring(self):
        return "TRAMPOLINE"
def jump(env, cont):
    return return_multi_vals(values.empty_vals, env, cont)

def return_value(w_val, env, cont):
    return return_multi_vals(values.Values.make([w_val]), env, cont)

def return_multi_vals(vals, env, cont):
    if cont is None:
        raise Done(vals)
    return cont.plug_reduce(vals)

class Cell(AST):
    _immutable_fields_ = ["expr", "need_cell_flags[*]"]
    def __init__(self, expr, need_cell_flags=None):
        if need_cell_flags is None:
            need_cell_flags = [True]
        self.expr = expr
        self.need_cell_flags = need_cell_flags

    def interpret(self, env, cont):
        return self.expr, env, CellCont(self, env, cont)

    def assign_convert(self, vars, env_structure):
        return Cell(self.expr.assign_convert(vars, env_structure))
    def mutated_vars(self):
        return self.expr.mutated_vars()
    def free_vars(self):
        return self.expr.free_vars()
    def tostring(self):
        return "Cell(%s)"%self.expr.tostring()

class Quote(AST):
    _immutable_fields_ = ["w_val"]
    simple = True
    def __init__ (self, w_val):
        self.w_val = w_val
    def interpret_simple(self, env):
        return self.w_val
    def assign_convert(self, vars, env_structure):
        return self
    def mutated_vars(self):
        return variable_set()
    def tostring(self):
        if isinstance(self.w_val, values.W_Bool) or isinstance(self.w_val, values.W_Number) or isinstance(self.w_val, values.W_String) or isinstance(self.w_val, values.W_Symbol):
            return "%s"%self.w_val.tostring()
        return "'%s"%self.w_val.tostring()

class QuoteSyntax(AST):
    _immutable_fields_ = ["w_val"]
    simple = True
    def __init__ (self, w_val):
        self.w_val = w_val
    def interpret_simple(self, env):
        return values.W_Syntax(self.w_val)
    def assign_convert(self, vars, env_structure):
        return self
    def mutated_vars(self):
        return variable_set()
    def tostring(self):
        return "#'%s"%self.w_val.tostring()

class VariableReference(AST):
    _immutable_fields_ = ["var", "is_mutable"]
    simple = True
    def __init__ (self, var, is_mutable=False):
        self.var = var
        self.is_mutable = is_mutable
    def interpret_simple(self, env):
        return values.W_VariableReference(self)
    def assign_convert(self, vars, env_structure):
        v = self.var
        if v and v in vars:
            return VariableReference(v, True)
        else:
            return self
    def mutated_vars(self):
        return variable_set()
    def tostring(self):
        return "#<#%variable-reference>"

class WithContinuationMark(AST):
    _immutable_fields_ = ["key", "value", "body"]

    def __init__(self, key, value, body):
        self.key = key
        self.value = value
        self.body = body

    def tostring(self):
        return "(with-continuation-mark %s %s %s)"%(self.key.tostring(),
                                                    self.value.tostring(),
                                                    self.body.tostring())

    def assign_convert(self, vars, env_structure):
        return WithContinuationMark(self.key.assign_convert(vars, env_structure),
                                    self.value.assign_convert(vars, env_structure),
                                    self.body.assign_convert(vars, env_structure))
    def mutated_vars(self):
        x = self.key.mutated_vars()
        for r in [self.value, self.body]:
            x.update(r.mutated_vars())
        return x
    def free_vars(self):
        x = self.key.free_vars()
        for r in [self.value, self.body]:
            x.update(r.free_vars())
        return x
    def interpret(self, env, cont):
        return self.key, env, WCMKeyCont(self, env, cont)

class App(AST):
    _immutable_fields_ = ["rator", "rands[*]", "remove_env"]


    def __init__ (self, rator, rands, remove_env=False):
        assert rator.simple
        for r in rands:
            assert r.simple
        self.rator = rator
        self.rands = rands
        self.remove_env = remove_env
        self.should_enter = isinstance(rator, ModuleVar)

    @staticmethod
    def make_let_converted(rator, rands):
        fresh_vars = []
        fresh_rhss = []
        new_rator = rator
        new_rands = []

        if not rator.simple:
            fresh_rator = Gensym.gensym("AppRator_")
            fresh_rator_var = LexicalVar(fresh_rator)
            fresh_rhss.append(rator)
            fresh_vars.append(fresh_rator)
            new_rator = fresh_rator_var

        for i, rand in enumerate(rands):
            if rand.simple:
                new_rands.append(rand)
            else:
                fresh_rand = Gensym.gensym("AppRand%s_"%i)
                fresh_rand_var = LexicalVar(fresh_rand)
                fresh_rhss.append(rand)
                fresh_vars.append(fresh_rand)
                new_rands.append(fresh_rand_var)
        # The body is an App operating on the freshly bound symbols
        if fresh_vars:
            fresh_body = [App(new_rator, new_rands[:], remove_env=True)]
            return Let(SymList(fresh_vars[:]), [1] * len(fresh_vars), fresh_rhss[:], fresh_body)
        else:
            return App(rator, rands)

    def assign_convert(self, vars, env_structure):
        return App(self.rator.assign_convert(vars, env_structure),
                   [e.assign_convert(vars, env_structure) for e in self.rands],
                   remove_env=self.remove_env)
    def mutated_vars(self):
        x = self.rator.mutated_vars()
        for r in self.rands:
            x.update(r.mutated_vars())
        return x
    def free_vars(self):
        x = self.rator.free_vars()
        for r in self.rands:
            x.update(r.free_vars())
        return x

    # Let conversion ensures that all the participants in an application
    # are simple.
    @jit.unroll_safe
    def interpret(self, env, cont):
        w_callable = self.rator.interpret_simple(env)
        args_w = [rand.interpret_simple(env) for rand in self.rands]
        if self.remove_env:
            # remove the env created by the let introduced by let_convert
            # it's no longer needed nor accessible
            # this whole stuff about the env seems useless in the App case,
            # because the callable will just ignore the passed in env. However,
            # we have a speculation in place in W_Procedure that checks whether
            # the closed over env is the same as the passed in one, which
            # breaks otherwise
            assert isinstance(env, ConsEnv)
            env = env.prev
        return w_callable.call(args_w, env, cont)

    def tostring(self):
        return "(%s %s)"%(self.rator.tostring(), " ".join([r.tostring() for r in self.rands]))

class SequencedBodyAST(AST):
    _immutable_fields_ = ["body[*]"]
    def __init__(self, body):
        assert isinstance(body, list)
        assert len(body) > 0
        self.body = body

    def make_begin_cont(self, env, prev, i=0):
        jit.promote(self)
        jit.promote(i)
        if i == len(self.body) - 1:
            return self.body[i], env, prev
        else:
            return self.body[i], env, BeginCont(self, i + 1, env, prev)


class Begin0(AST):
    _immutable_fields_ = ["first", "body"]
    @staticmethod
    def make(fst, rst):
        if rst:
            return Begin0(fst, Begin.make(rst))
        return fst
    def __init__(self, fst, rst):
        assert isinstance(rst, AST)
        self.first = fst
        self.body = rst
    def assign_convert(self, vars, env_structure):
        return Begin0(self.first.assign_convert(vars, env_structure), 
                      self.body.assign_convert(vars, env_structure))
    def free_vars(self):
        x = {}
        for r in [self.first, self.body]:
            x.update(r.free_vars())
        return x
    def mutated_vars(self):
        x = variable_set()
        for r in [self.first, self.body]:
            x.update(r.mutated_vars())
        return x
    def tostring(self):
        return "(begin0 %s %s)" % (self.first.tostring(), self.body.tostring())
    def interpret(self, env, cont):
        return self.first, env, Begin0Cont(self, env, cont)


class Begin(SequencedBodyAST):
    @staticmethod
    def make(body):
        if len(body) == 1:
            return body[0]
        else:
            return Begin(body)
    def assign_convert(self, vars, env_structure):
        return Begin.make([e.assign_convert(vars, env_structure) for e in self.body])
    def mutated_vars(self):
        x = variable_set()
        for r in self.body:
            x.update(r.mutated_vars())
        return x
    def free_vars(self):
        x = {}
        for r in self.body:
            x.update(r.free_vars())
        return x
    def interpret(self, env, cont):
        return self.make_begin_cont(env, cont)
    def tostring(self):
        return "(begin %s)" % (" ".join([e.tostring() for e in self.body]))

class Var(AST):
    _immutable_fields_ = ["sym", "env_structure"]
    simple = True
    def __init__ (self, sym, env_structure=None):
        assert isinstance(sym, values.W_Symbol)
        self.sym = sym
        self.env_structure = env_structure
    def interpret_simple(self, env):
        return self._lookup(env)
    def mutated_vars(self):
        return variable_set()
    def free_vars(self):
        x = {}
        x[self.sym] = None
        return x
    def tostring(self):
        return "%s"%variable_name(self.sym)


class CellRef(Var):
    def assign_convert(self, vars, env_structure):
        return CellRef(self.sym, env_structure)
    def tostring(self):
        return "CellRef(%s)"%variable_name(self.sym)
    def _set(self, w_val, env):
        v = env.lookup(self.sym, self.env_structure)
        assert isinstance(v, values.W_Cell)
        v.set_val(w_val)
    def _lookup(self, env):
        v = env.lookup(self.sym, self.env_structure)
        assert isinstance(v, values.W_Cell)
        return v.get_val()

class Gensym(object):

    class Counter(object):
        value = 0

    _counter = Counter()

    @staticmethod
    def gensym(hint="g"):
        Gensym._counter.value += 1
        # not using `make` so that it's really gensym
        return values.W_Symbol(hint + str(Gensym._counter.value))


class LexicalVar(Var):
    def _lookup(self, env):
        return env.lookup(self.sym, self.env_structure)
    def _set(self, w_val, env):
        assert 0
    def assign_convert(self, vars, env_structure):
        #assert isinstance(vars, r_dict)
        if self in vars:
            return CellRef(self.sym, env_structure)
        else:
            return LexicalVar(self.sym, env_structure)

class ModuleVar(Var):
    _immutable_fields_ = ["modenv"]
    def __init__(self, sym, srcmod, srcsym, env_structure=None):
        self.sym = sym
        self.srcmod = srcmod
        self.srcsym = srcsym
        self.env_structure = env_structure
        self.modenv = None
    def free_vars(self): return {}

    def _lookup(self, env):
        if self.modenv is None:
            self.modenv = env.toplevel_env.module_env
        return self._elidable_lookup()

    @jit.elidable
    def _elidable_lookup(self):
        assert self.modenv
        modenv = self.modenv
        if self.srcmod is None:
            mod = modenv.current_module
        elif self.srcmod == "#%kernel" or self.srcmod == "#%unsafe":
            # we don't separate these the way racket does
            # but maybe we should
            try:
                return prim_env[self.sym]
            except KeyError:
                raise SchemeException("can't find primitive %s" % (self.sym.tostring()))
        else:
            mod = modenv._find_module(self.srcmod)
        return mod.lookup(self.srcsym)

    def assign_convert(self, vars, env_structure):
        # we use None here for hashing because we don't have the module name in the
        # define-values when we need to look this up.
        if ModuleVar(self.sym, None, self.srcsym) in vars:
            return ModCellRef(self.sym, self.srcmod, self.srcsym)
        else:
            return self
    def _set(self, w_val, env): assert 0

class ModCellRef(Var):
    _immutable_fields_ = ["sym", "srcmod", "srcsym", "modvar"]

    def __init__(self, sym, srcmod, srcsym, env_structure=None):
        self.sym = sym
        self.srcmod = srcmod
        self.srcsym = srcsym
        self.modvar = ModuleVar(self.sym, self.srcmod, self.srcsym)
    def assign_convert(self, vars, env_structure):
        return ModCellRef(self.sym, self.srcmod, self.srcsym)
    def tostring(self):
        return "ModCellRef(%s)"%variable_name(self.sym)
    def _set(self, w_val, env):
        w_res = self.modvar._lookup(env)
        assert isinstance(w_res, values.W_Cell)
        w_res.set_val(w_val)
    def _lookup(self, env):
        w_res = self.modvar._lookup(env)
        assert isinstance(w_res, values.W_Cell)
        return w_res.get_val()
    def to_modvar(self):
        # we use None here for hashing because we don't have the module name in the
        # define-values when we need to look this up.
        return ModuleVar(self.sym, None, self.srcsym)


class ToplevelVar(Var):
    def _lookup(self, env):
        return env.toplevel_env.toplevel_lookup(self.sym)
    def assign_convert(self, vars, env_structure):
        return self
    def _set(self, w_val, env):
        env.toplevel_env.toplevel_set(self.sym, w_val)

class SymList(object):
    _immutable_fields_ = ["elems[*]", "prev"]
    def __init__(self, elems, prev=None):
        assert isinstance(elems, list)
        self.elems = elems
        self.prev = prev

class SetBang(AST):
    _immutable_fields_ = ["var", "rhs"]
    def __init__(self, var, rhs):
        self.var = var
        self.rhs = rhs
    def interpret(self, env, cont):
        return self.rhs, env, SetBangCont(self, env, cont)
    def assign_convert(self, vars, env_structure):
        return SetBang(self.var.assign_convert(vars, env_structure),
                       self.rhs.assign_convert(vars, env_structure))
    def mutated_vars(self):
        x = self.rhs.mutated_vars()
        if isinstance(self.var, CellRef):
            x[LexicalVar(self.var.sym)] = None
        elif isinstance(self.var, ModCellRef):
            x[self.var.to_modvar()] = None
        # do nothing for top-level vars, they're all mutated
        return x
    def free_vars(self):
        x = self.rhs.free_vars()
        if isinstance(self.var, CellRef):
            x[self.var.sym] = None
        return x
    def tostring(self):
        return "(set! %s %s)"%(variable_name(self.var.sym), self.rhs.tostring())

class If(AST):
    _immutable_fields_ = ["tst", "thn", "els", "remove_env"]
    def __init__ (self, tst, thn, els, remove_env=False):
        assert tst.simple
        self.tst = tst
        self.thn = thn
        self.els = els
        self.remove_env = remove_env

    @staticmethod
    def make_let_converted(tst, thn, els):
        if tst.simple:
            return If(tst, thn, els)
        else:
            fresh = Gensym.gensym("if_")
            return Let(SymList([fresh]),
                       [1],
                       [tst],
                       [If(LexicalVar(fresh), thn, els, remove_env=True)])

    def interpret(self, env, cont):
        w_val = self.tst.interpret_simple(env)
        if self.remove_env:
            # remove the env created by the let introduced by make_let_converted
            # it's no longer needed nor accessible
            assert env._get_size_list() == 1
            assert isinstance(env, ConsEnv)
            env = env.prev
        if w_val is values.w_false:
            return self.els, env, cont
        else:
            return self.thn, env, cont

    def assign_convert(self, vars, env_structure):
        if self.remove_env:
            sub_env_structure = env_structure.prev
        else:
            sub_env_structure = env_structure
        return If(self.tst.assign_convert(vars, env_structure),
                  self.thn.assign_convert(vars, sub_env_structure),
                  self.els.assign_convert(vars, sub_env_structure),
                  remove_env=self.remove_env)
    def mutated_vars(self):
        x = variable_set()
        for b in [self.tst, self.els, self.thn]:
            x.update(b.mutated_vars())
        return x
    def free_vars(self):
        x = {}
        for b in [self.tst, self.els, self.thn]:
            x.update(b.free_vars())
        return x
    def tostring(self):
        return "(if %s %s %s)"%(self.tst.tostring(), self.thn.tostring(), self.els.tostring())


def make_lambda(formals, rest, body):
    args = SymList(formals + ([rest] if rest else []))
    frees = SymList(free_vars_lambda(body, args).keys())
    args = SymList(args.elems, frees)
    return Lambda(formals, rest, args, frees, body)

def free_vars_lambda(body, args):
    x = {}
    for b in body:
        x.update(b.free_vars())
    for v in args.elems:
        if v in x:
            del x[v]
    return x



class CaseLambda(AST):
    _immutable_fields_ = ["lams[*]", "any_frees", "w_closure_if_no_frees?"]
    simple = True
    def __init__(self, lams, recursive_sym=None):
        ## TODO: drop lams whose arity is redundant
        ## (case-lambda [x 0] [(y) 1]) == (lambda x 0)
        self.lams = lams
        self.any_frees = False
        for l in lams:
            if l.frees.elems:
                self.any_frees = True
                break
        self.w_closure_if_no_frees = None
        self.recursive_sym = recursive_sym

    def make_recursive_copy(self, sym):
        return CaseLambda(self.lams, sym)

    def interpret_simple(self, env):
        if not self.any_frees:
            # cache closure if there are no free variables and the toplevel env
            # is the same as last time
            w_closure = self.w_closure_if_no_frees
            if w_closure is None:
                w_closure = values.W_PromotableClosure(self, env.toplevel_env)
                self.w_closure_if_no_frees = w_closure
            else:
                assert w_closure.closure._get_list(0).toplevel_env is env.toplevel_env
            return w_closure
        return values.W_Closure.make(self, env)
    def free_vars(self):
        result = {}
        for l in self.lams:
            result.update(l.free_vars())
        if self.recursive_sym in result:
            del result[self.recursive_sym]
        return result
    def mutated_vars(self):
        x = variable_set()
        for l in self.lams:
            x.update(l.mutated_vars())
        return x
    def assign_convert(self, vars, env_structure):
        ls = [l.assign_convert(vars, env_structure) for l in self.lams]
        return CaseLambda(ls, recursive_sym=self.recursive_sym)
    def tostring(self):
        if len(self.lams) == 1:
            return self.lams[0].tostring()
        return "(case-lambda %s)"%(" ".join([l.tostring() for l in self.lams]))

class Lambda(SequencedBodyAST):
    _immutable_fields_ = ["formals[*]", "rest", "args",
                          "frees", "enclosing_env_structure",
                          ]
    simple = True
    def __init__ (self, formals, rest, args, frees, body, enclosing_env_structure=None):
        SequencedBodyAST.__init__(self, body)
        self.formals = formals
        self.rest = rest
        self.args = args
        self.frees = frees
        self.enclosing_env_structure = enclosing_env_structure

    # returns n for fixed arity, -(n+1) for arity-at-least n
    # my kingdom for Either
    def get_arity(self):
        if self.rest:
            return -(len(self.formals)+1)
        else:
            return len(self.formals)

    def interpret_simple(self, env):
        assert False # unreachable

    def assign_convert(self, vars, env_structure):
        local_muts = variable_set()
        for b in self.body:
            local_muts.update(b.mutated_vars())
        new_lets = []
        new_vars = vars.copy()
        for i in self.args.elems:
            li = LexicalVar(i)
            if li in new_vars:
                del new_vars[li]
            if li in local_muts:
                new_lets.append(i)
        for k, v in local_muts.iteritems():
            new_vars[k] = v
        if new_lets:
            sub_env_structure = SymList(new_lets, self.args)
        else:
            sub_env_structure = self.args
        new_body = [b.assign_convert(new_vars, sub_env_structure) for b in self.body]
        if new_lets:
            cells = [Cell(LexicalVar(v, self.args)) for v in new_lets]
            new_body = [Let(sub_env_structure, [1] * len(new_lets), cells, new_body)]
        return Lambda(self.formals, self.rest, self.args, self.frees, new_body, env_structure)
    def mutated_vars(self):
        x = variable_set()
        for b in self.body:
            x.update(b.mutated_vars())
        for v in self.args.elems:
            lv = LexicalVar(v)
            if lv in x:
                del x[lv]
        return x
    def free_vars(self):
        result = free_vars_lambda(self.body, self.args)
        return result

    def match_args(self, args):
        fmls_len = len(self.formals)
        args_len = len(args)
        if fmls_len != args_len and not self.rest:
            raise SchemeException("wrong number of arguments to %s, expected %s but got %s"%(self.tostring(), fmls_len,args_len))
        if fmls_len > args_len:
            raise SchemeException("wrong number of arguments to %s, expected at least %s but got %s"%(self.tostring(), fmls_len,args_len))
        if self.rest:
            actuals = args[0:fmls_len] + [values.to_list(args[fmls_len:])]
        else:
            actuals = args
        return actuals


    def tostring(self):
        if self.rest and (not self.formals):
            return "(lambda %s %s)"%(self.rest, [b.tostring() for b in self.body])
        if self.rest:
            return "(lambda (%s . %s) %s)"%(self.formals, self.rest, [b.tostring() for b in self.body])
        else:
            return "(lambda (%s) %s)"%(" ".join([variable_name(v) for v in self.formals]),
                                       self.body[0].tostring() if len(self.body) == 1 else
                                       " ".join([b.tostring() for b in self.body]))


class Letrec(SequencedBodyAST):
    _immutable_fields_ = ["args", "rhss[*]", "counts[*]", "total_counts[*]"]
    def __init__(self, args, counts, rhss, body):
        assert len(counts) > 0 # otherwise just use a begin
        assert isinstance(args, SymList)
        SequencedBodyAST.__init__(self, body)
        self.counts = counts
        total_counts = []
        total_count = 0
        for i, count in enumerate(counts):
            total_counts.append(total_count)
            total_count += count
        self.total_counts = total_counts[:] # copy to make fixed-size
        self.rhss = rhss
        self.args = args
    @jit.unroll_safe
    def interpret(self, env, cont):
        env_new = ConsEnv.make([values.W_Cell(None) for var in self.args.elems], env, env.toplevel_env)
        return self.rhss[0], env_new, LetrecCont(self, 0, env_new, cont)
    def mutated_vars(self):
        x = variable_set()
        for b in self.body + self.rhss:
            x.update(b.mutated_vars())
        for v in self.args.elems:
            lv = LexicalVar(v)
            x[lv] = None
        return x
    def free_vars(self):
        x = {}
        for b in self.body + self.rhss:
            x.update(b.free_vars())
        for v in self.args.elems:
            if v in x:
                del x[v]
        return x
    def assign_convert(self, vars, env_structure):
        local_muts = variable_set()
        for b in self.body + self.rhss:
            local_muts.update(b.mutated_vars())
        for v in self.args.elems:
            lv = LexicalVar(v)
            local_muts[lv] = None
        new_vars = vars.copy()
        for k, v in local_muts.iteritems():
            new_vars[k] = v
        sub_env_structure = SymList(self.args.elems, env_structure)
        new_rhss = [rhs.assign_convert(new_vars, sub_env_structure) for rhs in self.rhss]
        new_body = [b.assign_convert(new_vars, sub_env_structure) for b in self.body]
        return Letrec(sub_env_structure, self.counts, new_rhss, new_body)
    def tostring(self):
        return "(letrec (%s) %s)"%([(variable_name(v),self.rhss[i].tostring()) for i, v in enumerate(self.args.elems)],
                                   [b.tostring() for b in self.body])

def _make_symlist_counts(varss):
    counts = []
    argsl = []
    for vars in varss:
        counts.append(len(vars))
        argsl += vars
    argsl = argsl[:] # copy to make fixed-size
    return SymList(argsl), counts

def make_let(varss, rhss, body):
    if not varss:
        return Begin.make(body)
    if 1 == len(varss) and 1 == len(varss[0]):
        return make_let_singlevar(varss[0][0], rhss[0], body)
    symlist, counts = _make_symlist_counts(varss)
    return Let(symlist, counts, rhss, body)

def make_let_singlevar(sym, rhs, body):
    if 1 == len(body):
        b, = body
        if isinstance(b, LexicalVar) and sym is b.sym:
            return rhs
        elif isinstance(b, App):
            rator = b.rator
            x = {}
            for rand in b.rands:
                x.update(rand.free_vars())
            if (isinstance(rator, LexicalVar) and
                    sym is rator.sym and
                    rator.sym not in x):
                assert not b.remove_env
                return App.make_let_converted(rhs, b.rands)
        elif isinstance(b, If):
            tst = b.tst
            if (isinstance(tst, LexicalVar) and tst.sym is sym and
                    sym not in b.thn.free_vars() and
                    sym not in b.els.free_vars() and
                    rhs.simple):
                return If(rhs, b.thn, b.els)
    return Let(SymList([sym]), [1], [rhs], body)

def make_letrec(varss, rhss, body):
    if not varss:
        return Begin.make(body)
    if 1 == len(varss) and 1 == len(varss[0]):
        rhs = rhss[0]
        sym = varss[0][0]
        if isinstance(rhs, CaseLambda) and LexicalVar(sym) not in rhs.mutated_vars():
            reclambda = rhs.make_recursive_copy(sym)
            return make_let_singlevar(sym, reclambda, body)

    symlist, counts = _make_symlist_counts(varss)
    return Letrec(symlist, counts, rhss, body)


class Let(SequencedBodyAST):
    _immutable_fields_ = ["rhss[*]", "args", "counts[*]"]
    def __init__(self, args, counts, rhss, body):
        SequencedBodyAST.__init__(self, body)
        assert len(counts) > 0 # otherwise just use a begin
        assert isinstance(args, SymList)
        self.counts = counts
        self.rhss = rhss
        self.args = args

    def interpret(self, env, cont):
        return self.rhss[0], env, LetCont.make([], self, 0, env, cont)

    def mutated_vars(self):
        x = variable_set()
        for b in self.body:
            x.update(b.mutated_vars())
        for v in self.args.elems:
            lv = LexicalVar(v)
            if lv in x:
                del x[lv]
        for b in self.rhss:
            x.update(b.mutated_vars())
        return x
    def free_vars(self):
        x = {}
        for b in self.body:
            x.update(b.free_vars())
        for v in self.args.elems:
            if v in x:
                del x[v]
        for b in self.rhss:
            x.update(b.free_vars())
        return x
    def assign_convert(self, vars, env_structure):
        local_muts = variable_set()
        for b in self.body:
            local_muts.update(b.mutated_vars())
        new_rhss = []
        for i, rhs in enumerate(self.rhss):
            new_rhs = rhs.assign_convert(vars, env_structure)
            need_cell_flags = [(LexicalVar(self.args.elems[i + j]) in local_muts)
                               for j in range(self.counts[i])]
            if True in need_cell_flags:
                new_rhs = Cell(new_rhs, need_cell_flags)
            new_rhss.append(new_rhs)

        new_vars = vars.copy()
        for k, v in local_muts.iteritems():
            new_vars[k] = v
        sub_env_structure = SymList(self.args.elems, env_structure)
        new_body = [b.assign_convert(new_vars, sub_env_structure) for b in self.body]
        return Let(sub_env_structure, self.counts, new_rhss, new_body)

    def tostring(self):
        result = ["(let ("]
        j = 0
        for i, count in enumerate(self.counts):
            result.append("[")
            if count > 1:
                result.append("(")
            for _ in range(count):
                result.append(variable_name(self.args.elems[j]))
                j += 1
            if count > 1:
                result.append(")")
            result.append(" ")
            result.append(self.rhss[i].tostring())
            result.append("]")
        result.append(") ")
        result.append(" ".join([b.tostring() for b in self.body]))
        result.append(")")
        return "".join(result)


class DefineValues(AST):
    _immutable_fields_ = ["names", "rhs"]
    names = []
    rhs = Quote(values.w_null)

    def __init__(self, ns, r):
        self.names = ns
        self.rhs = r

    def defined_vars(self):
        defs = {} # a dictionary, contains symbols
        for n in self.names:
            defs[n] = None
        return defs

    def interpret(self, env, cont):
        return self.rhs.interpret(env, cont)

    def assign_convert(self, vars, env_structure):
        mut = False
        need_cell_flags = [(ModuleVar(i, None, i) in vars) for i in self.names]
        if (True in need_cell_flags):
            return DefineValues(self.names, Cell(self.rhs.assign_convert(vars, env_structure),
                                                 need_cell_flags))
        else:
            return DefineValues(self.names, self.rhs.assign_convert(vars, env_structure))
    def mutated_vars(self):
        return self.rhs.mutated_vars()
    def free_vars(self):
        # free_vars doesn't contain module-bound variables
        # which is the only thing defined by define-values
        return self.rhs.free_vars()
    def tostring(self):
        return "(define-values %s %s)"%(self.names, self.rhs.tostring())

def get_printable_location(green_ast):
    if green_ast is None:
        return 'Green_Ast is None'
    return green_ast.tostring()
driver = jit.JitDriver(reds=["env", "cont"],
                       greens=["ast"],
                       get_printable_location=get_printable_location)

def interpret_one(ast, env=None):
    #import pdb
    #pdb.set_trace()
    cont = None
    if not env:
        env = ToplevelEnv()
    try:
        while True:
            driver.jit_merge_point(ast=ast, env=env, cont=cont)
            ast, env, cont = ast.interpret(env, cont)
            if ast.should_enter:
#                print ast.tostring()
                driver.can_enter_jit(ast=ast, env=env, cont=cont)
    except Done, e:
        return e.values

def interpret_toplevel(a, env):
    if isinstance(a, Begin):
        x = None
        for a2 in a.body:
            x = interpret_toplevel(a2, env)
        return x
    elif isinstance(a, DefineValues):
        assert 0 # FIXME
        env.toplevel_env.toplevel_set(a.name, interpret_one(a.rhs, env))
        return values.Values.make([values.w_void])
    else:
        return interpret_one(a, env)

def interpret_module(m, env=None):
    env = env if env else ToplevelEnv()
    m.interpret_mod(env)
    return m

def interpret(asts):
    env = ToplevelEnv()
    x = None
    for a in asts:
        x = interpret_toplevel(a, env)
    return x
