//! egg port of benchmarks/horner/horner-<n>.egg (Horner's method, from foresight's examples/poly).
//! Same language (Peano exponents), the same 11 rewrites, the polynomial of degree n built like
//! gen.py does, run to saturation: iterations until one changes nothing (that iteration counts).
//! Usage: horner --instance N [--report FILE]
use egg::{rewrite as rw, *};
use std::time::Instant;

define_language! {
    pub enum Arith {
        "Zero" = Zero,
        "Succ" = Succ(Id),
        "Add" = Add([Id; 2]),
        "Mul" = Mul([Id; 2]),
        "Pow" = Pow([Id; 2]),
        Var(Symbol),
    }
}

type EGraph = egg::EGraph<Arith, ()>;

#[rustfmt::skip]
fn rules() -> Vec<Rewrite<Arith, ()>> { vec![
    rw!("add-commutativity";  "(Add ?x ?y)" => "(Add ?y ?x)"),
    rw!("mul-commutativity";  "(Mul ?x ?y)" => "(Mul ?y ?x)"),
    rw!("add-associativity1"; "(Add (Add ?x ?y) ?z)" => "(Add ?x (Add ?y ?z))"),
    rw!("add-associativity2"; "(Add ?x (Add ?y ?z))" => "(Add (Add ?x ?y) ?z)"),
    rw!("mul-associativity1"; "(Mul (Mul ?x ?y) ?z)" => "(Mul ?x (Mul ?y ?z))"),
    rw!("mul-associativity2"; "(Mul ?x (Mul ?y ?z))" => "(Mul (Mul ?x ?y) ?z)"),
    rw!("distributivity1";    "(Mul ?x (Add ?a ?b))" => "(Add (Mul ?x ?a) (Mul ?x ?b))"),
    rw!("distributivity2";    "(Add (Mul ?x ?a) (Mul ?x ?b))" => "(Mul ?x (Add ?a ?b))"),
    rw!("multiplicative-identity"; "(Mul ?x (Succ Zero))" => "?x"),
    rw!("power-null";         "(Pow ?x Zero)" => "(Succ Zero)"),
    rw!("power-reduction";    "(Pow ?x (Succ ?n))" => "(Mul ?x (Pow ?x ?n))"),
]}

fn constant(i: usize) -> String {
    if i == 0 { "Zero".into() } else { format!("(Succ {})", constant(i - 1)) }
}

/// ((a_n x^n + a_{n-1} x^{n-1}) + ...) + a_0, coefficients a, b, c, ... = a_0, a_1, ...
fn polynomial(n: usize) -> String {
    let coeff = |i: usize| ((b'a' + i as u8) as char).to_string();
    let mut terms: Vec<String> = (0..=n)
        .map(|i| if i == 0 { coeff(0) } else { format!("(Mul {} (Pow x {}))", coeff(i), constant(i)) })
        .collect();
    terms.reverse();
    terms.into_iter().reduce(|acc, t| format!("(Add {acc} {t})")).unwrap()
}

pub fn saturate(egraph: EGraph, rules: &[Rewrite<Arith, ()>]) -> Runner<Arith, ()> {
    Runner::default()
        .with_explanations_disabled()
        .with_scheduler(SimpleScheduler)
        .with_iter_limit(usize::MAX)
        .with_node_limit(usize::MAX)
        .with_time_limit(std::time::Duration::from_secs(u64::MAX / 4))
        .with_egraph(egraph)
        .run(rules)
}

pub fn report_sizes<L: Language, N: Analysis<L>>(egraph: &egg::EGraph<L, N>, name: impl Fn(&L) -> String) {
    let mut sizes = std::collections::BTreeMap::<String, usize>::new();
    for class in egraph.classes() {
        for node in &class.nodes {
            *sizes.entry(name(node)).or_default() += 1;
        }
    }
    println!("(");
    for (k, n) in &sizes {
        println!(" ({k} {n})");
    }
    println!(")");
}

fn main() {
    let mut instance = 5usize;
    let mut report = None;
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--instance" => instance = args.next().unwrap().parse().unwrap(),
            "--report" => report = Some(args.next().unwrap()),
            _ => panic!("unknown argument {a}"),
        }
    }
    let rules = rules();
    let mut egraph = EGraph::default();
    egraph.add_expr(&polynomial(instance).parse().unwrap());
    egraph.rebuild();
    let t0 = Instant::now();
    let runner = saturate(egraph, &rules);
    let run_s = t0.elapsed().as_secs_f64();
    assert!(matches!(runner.stop_reason, Some(StopReason::Saturated)), "{:?}", runner.stop_reason);
    println!("iterations {}", runner.iterations.len());
    println!("run_s {run_s}");
    report_sizes(&runner.egraph, |n| match n {
        Arith::Zero => "Zero", Arith::Succ(_) => "Succ", Arith::Add(_) => "Add",
        Arith::Mul(_) => "Mul", Arith::Pow(_) => "Pow", Arith::Var(_) => "Var",
    }.to_string());
    eprintln!("egg: horner-{instance}: {} iterations, {} e-nodes, {} e-classes, {run_s:.3}s",
        runner.iterations.len(), runner.egraph.total_number_of_nodes(), runner.egraph.number_of_classes());
    if let Some(p) = report {
        std::fs::write(p, format!("{{\"run_s\": {run_s}, \"iterations\": {}}}", runner.iterations.len())).unwrap();
    }
}
