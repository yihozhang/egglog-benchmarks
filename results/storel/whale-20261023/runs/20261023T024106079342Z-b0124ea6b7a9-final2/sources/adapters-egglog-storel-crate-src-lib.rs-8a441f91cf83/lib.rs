//! egglog extension for the STOREL benchmark (benchmarks/storel): STOREL's index shifting and
//! beta reduction on the smallest term of a class, in Rust, as egg's custom appliers do with
//! their `beta_extract` analysis. The program keeps computing that term with rules
//! (`best : Term -> Frozen :merge (frozen-min old new)`); the extension provides
//!
//! - `Frozen`, a base sort of immutable de Bruijn terms over the SDQL signature of gen.py:
//!   `(fvar i)`, `(fnum n)`, `(fsym s)`, `(f1 op a)`, `(f2 op a b)`, `(f3 op a b c)` build
//!   terms (`op` is the Term constructor's name), `(frozen-size f)` and `(frozen-hash f)` read
//!   the size and the structural hash HASH of gen.py (cached at construction),
//!   `(frozen-min a b)` is the smaller term by (size, hash), `(fshift cutoff delta f)` moves
//!   the free indices >= cutoff by delta and `(fsubst i a f)` replaces %i by `a` (shifted up
//!   by i) and renumbers %j>i to %j-1;
//! - `(thaw f)`, which returns the e-class of `f` after adding its e-nodes to the Term tables.
//!
//! `thaw` writes the e-graph, which egglog admits only in `:naive` or `:unsafe-seminaive`
//! rules (the latter on the program's guarantee that the action depends on nothing but its
//! query-bound values, which holds here: frozen terms are immutable).

use std::hash::{Hash, Hasher};
use std::sync::Arc;

use egglog::api::RawValues;
use egglog::ast::{Literal, Span};
use egglog::constraint::{self, Constraint, ImpossibleConstraint, TypeConstraint};
use egglog::prelude::{BaseSort, add_base_sort};
use egglog::sort::{BaseValues, Boxed, S};
use egglog::{
    ArcSort, Atom, AtomTerm, Core, EGraph, FullPrim, FullState, Primitive, TermDag, TermId,
    TypeInfo, Value, Write, add_primitive, span,
};

/// The constructors of the Term datatype in gen.py's order, with the number of variables each
/// child binds. Opcodes for HASH: Var 1, Num 2, Sym 3, then 4 + index.
const CTORS: &[(&str, &[i64])] = &[
    ("Mul", &[0, 0]), ("Add", &[0, 0]), ("Sub", &[0, 0]), ("Eq", &[0, 0]),
    ("Let", &[0, 1]), ("Sing", &[0, 0]), ("IfThen", &[0, 0]), ("Get", &[0, 0]),
    ("Sum", &[0, 2]), ("Merge", &[0, 0, 3]), ("Range", &[0, 0]), ("SubArray", &[0, 0, 0]),
    ("App", &[0, 0]), ("Binop", &[0, 0, 0]), ("Lambda", &[1]), ("Unique", &[0]),
];
/// The symbols the rules create (`(Sym s)`), in gen.py's order.
const SYMBOLS: &[&str] = &["mult", "add", "sub", "getf", "singf", "uniquef"];
/// The base sort's name.
const FROZEN: &str = "Frozen";

// HASH of gen.py: (op * 4099 + sum child_i * P[i]) mod 2^48, leaves hashing their index,
// number (low 32 bits) or symbol code as their only "child".
const HASH_MASK: u64 = (1 << 48) - 1;
const HASH_OP: u64 = 4099;
const HASH_P: [u64; 3] = [8191, 12281, 16381];

#[derive(Debug, Clone, PartialEq, Eq)]
enum Op {
    Var(i64),
    Num(i64),
    Sym(usize),
    Ctor(usize),
}

#[derive(Debug)]
struct Node {
    op: Op,
    kids: Vec<FTerm>,
    size: i64,
    hash: u64,
}

/// A frozen term: an immutable tree with its size and hash cached. Equality is structural
/// (with the hash as a fast reject), so egglog's interning dedups equal terms.
#[derive(Debug, Clone)]
pub struct FTerm(Arc<Node>);

/// The egglog base value of the `F` sort.
pub type FT = Boxed<FTerm>;

impl PartialEq for FTerm {
    fn eq(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.0, &other.0)
            || (self.0.hash == other.0.hash && self.0.op == other.0.op && self.0.kids == other.0.kids)
    }
}

impl Eq for FTerm {}

impl Hash for FTerm {
    fn hash<H: Hasher>(&self, state: &mut H) {
        state.write_u64(self.0.hash);
    }
}

impl FTerm {
    fn new(op: Op, kids: Vec<FTerm>) -> FTerm {
        let (code, leaf) = match &op {
            Op::Var(i) => (1, Some(*i as u64)),
            Op::Num(n) => (2, Some((*n as u64) & 0xFFFF_FFFF)),
            Op::Sym(k) => (3, Some(*k as u64)),
            Op::Ctor(c) => (4 + *c as u64, None),
        };
        let mut hash = code.wrapping_mul(HASH_OP);
        if let Some(v) = leaf {
            hash = hash.wrapping_add(v.wrapping_mul(HASH_P[0])) & HASH_MASK;
        }
        for (kid, p) in kids.iter().zip(HASH_P.iter()) {
            hash = hash.wrapping_add(kid.0.hash.wrapping_mul(*p)) & HASH_MASK;
        }
        let size = 1 + kids.iter().map(|k| k.0.size).sum::<i64>();
        FTerm(Arc::new(Node { op, kids, size, hash: hash & HASH_MASK }))
    }

    fn leaf(op: Op) -> FTerm {
        FTerm::new(op, vec![])
    }

    fn ctor(name: &str, kids: Vec<FTerm>) -> Option<FTerm> {
        let c = CTORS.iter().position(|(n, _)| *n == name)?;
        (CTORS[c].1.len() == kids.len()).then(|| FTerm::new(Op::Ctor(c), kids))
    }

    /// STOREL's `shift_mut`: free indices >= cutoff move by delta; binders raise the cutoff.
    fn shift(&self, cutoff: i64, delta: i64) -> FTerm {
        match &self.0.op {
            Op::Var(j) if *j >= cutoff => FTerm::leaf(Op::Var(j + delta)),
            Op::Var(_) | Op::Num(_) | Op::Sym(_) => self.clone(),
            Op::Ctor(c) => {
                let kids = self.0.kids.iter().zip(CTORS[*c].1).map(|(k, b)| k.shift(cutoff + b, delta)).collect();
                FTerm::new(Op::Ctor(*c), kids)
            }
        }
    }

    /// STOREL's `beta_reduce` generalized to depth `index`: %index becomes `arg` shifted up by
    /// index, %j > index becomes %j-1; binders raise the index.
    fn subst(&self, index: i64, arg: &FTerm) -> FTerm {
        match &self.0.op {
            Op::Var(j) if *j < index => self.clone(),
            Op::Var(j) if *j == index => if index == 0 { arg.clone() } else { arg.shift(0, index) },
            Op::Var(j) => FTerm::leaf(Op::Var(j - 1)),
            Op::Num(_) | Op::Sym(_) => self.clone(),
            Op::Ctor(c) => {
                let kids = self.0.kids.iter().zip(CTORS[*c].1).map(|(k, b)| k.subst(index + b, arg)).collect();
                FTerm::new(Op::Ctor(*c), kids)
            }
        }
    }

    /// The term in the egg port's syntax, for printing.
    fn render(&self) -> String {
        match &self.0.op {
            Op::Var(i) => format!("%{i}"),
            Op::Num(n) => n.to_string(),
            Op::Sym(k) => SYMBOLS[*k].to_string(),
            Op::Ctor(c) => {
                let kids: Vec<String> = self.0.kids.iter().map(FTerm::render).collect();
                format!("({} {})", CTORS[*c].0, kids.join(" "))
            }
        }
    }

    /// Adds the term's e-nodes to the Term tables and returns the root's e-class.
    fn thaw(&self, state: &mut FullState<'_, '_>) -> Result<Value, egglog::Error> {
        let base = state.base_values();
        match &self.0.op {
            Op::Var(i) => state.add("Var", RawValues(vec![base.get(*i)])),
            Op::Num(n) => state.add("Num", RawValues(vec![base.get(*n)])),
            Op::Sym(k) => state.add("Sym", RawValues(vec![base.get(S::new(SYMBOLS[*k].to_string()))])),
            Op::Ctor(c) => {
                let mut args = Vec::with_capacity(self.0.kids.len());
                for kid in &self.0.kids {
                    args.push(kid.thaw(state)?);
                }
                state.add(CTORS[*c].0, RawValues(args))
            }
        }
    }
}

/// The `Frozen` base sort and its pure primitives.
#[derive(Debug)]
pub struct FrozenSort;

impl BaseSort for FrozenSort {
    type Base = FT;

    fn name(&self) -> &str {
        FROZEN
    }

    #[rustfmt::skip]
    fn register_primitives(&self, eg: &mut EGraph) {
        add_primitive!(eg, "fvar" = |i: i64| -> FT { FT::new(FTerm::leaf(Op::Var(i))) });
        add_primitive!(eg, "fnum" = |n: i64| -> FT { FT::new(FTerm::leaf(Op::Num(n))) });
        add_primitive!(eg, "fsym" = |s: S| -?> FT {
            SYMBOLS.iter().position(|x| *x == s.0.as_str()).map(|k| FT::new(FTerm::leaf(Op::Sym(k))))
        });
        add_primitive!(eg, "f1" = |op: S, a: FT| -?> FT { FTerm::ctor(&op.0, vec![a.0]).map(FT::new) });
        add_primitive!(eg, "f2" = |op: S, a: FT, b: FT| -?> FT { FTerm::ctor(&op.0, vec![a.0, b.0]).map(FT::new) });
        add_primitive!(eg, "f3" = |op: S, a: FT, b: FT, c: FT| -?> FT { FTerm::ctor(&op.0, vec![a.0, b.0, c.0]).map(FT::new) });
        add_primitive!(eg, "frozen-min" = |a: FT, b: FT| -> FT {
            if (a.0.0.size, a.0.0.hash) <= (b.0.0.size, b.0.0.hash) { a } else { b }
        });
        add_primitive!(eg, "frozen-size" = |a: FT| -> i64 { a.0.0.size });
        add_primitive!(eg, "frozen-hash" = |a: FT| -> i64 { a.0.0.hash as i64 });
        add_primitive!(eg, "fshift" = |cutoff: i64, delta: i64, a: FT| -> FT { FT::new(a.0.shift(cutoff, delta)) });
        add_primitive!(eg, "fsubst" = |index: i64, arg: FT, body: FT| -> FT { FT::new(body.0.subst(index, &arg.0)) });
    }

    fn reconstruct_termdag(&self, base_values: &BaseValues, value: Value, termdag: &mut TermDag) -> TermId {
        let term = base_values.unwrap::<FT>(value);
        termdag.lit(Literal::String(term.0.render()))
    }
}

/// `(thaw f) : Frozen -> Term`: the e-class of frozen term `f` after adding its e-nodes to the
/// Term tables.
#[derive(Clone, Copy)]
pub struct Thaw;

impl Primitive for Thaw {
    fn name(&self) -> &str {
        "thaw"
    }

    fn get_type_constraints(&self, span: &Span) -> Box<dyn TypeConstraint> {
        Box::new(ThawTypeConstraint { span: span.clone() })
    }
}

impl FullPrim for Thaw {
    fn apply<'a, 'db>(&self, mut state: FullState<'a, 'db>, args: &[Value]) -> Option<Value> {
        let [f] = args else {
            panic!("thaw takes one frozen term; the typechecker admitted {} arguments", args.len())
        };
        let term = state.base_values().unwrap::<FT>(*f);
        match term.0.thaw(&mut state) {
            Ok(id) => Some(id),
            Err(err) => {
                log::error!("thaw: {err}");
                state.panic();
                None
            }
        }
    }
}

/// `(thaw Frozen) : Term`, where `Term` is the program's eq-sort of that name.
struct ThawTypeConstraint {
    span: Span,
}

impl TypeConstraint for ThawTypeConstraint {
    fn get(&self, arguments: &[AtomTerm], typeinfo: &TypeInfo) -> Vec<Box<dyn Constraint<AtomTerm, ArcSort>>> {
        let sort = |name: &str| typeinfo.get_arcsorts_by(|sort| sort.name() == name).into_iter().next();
        let ([f, out], Some(frozen), Some(term)) = (arguments, sort(FROZEN), sort("Term")) else {
            return vec![constraint::impossible(ImpossibleConstraint::ArityMismatch {
                atom: Atom { span: self.span.clone(), head: "thaw".to_owned(), args: arguments.to_vec() },
                expected: 2,
            })];
        };
        vec![constraint::assign(f.clone(), frozen), constraint::assign(out.clone(), term)]
    }
}

/// Registers the `Frozen` sort, its primitives and `thaw`.
pub fn register(egraph: &mut EGraph) {
    add_base_sort(egraph, FrozenSort, span!()).expect("register the Frozen sort");
    egraph.add_full_primitive(Thaw, None);
}
