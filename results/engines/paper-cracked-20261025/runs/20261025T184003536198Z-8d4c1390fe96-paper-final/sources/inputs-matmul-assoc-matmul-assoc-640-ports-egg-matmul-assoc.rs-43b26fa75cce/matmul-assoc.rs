//! egg port of benchmarks/matmul-assoc/matmul-assoc-<n>.egg (from foresight's examples/mm):
//! a chain of n multiplications of (Mat 10 10), the two associativity rules, run to saturation.
//! Usage: matmul-assoc --instance N [--report FILE]
use egg::{rewrite as rw, *};
use std::time::Instant;

define_language! {
    pub enum Linalg {
        "Mat" = Mat([Id; 2]),
        "Mul" = Mul([Id; 2]),
        Num(i64),
    }
}

type EGraph = egg::EGraph<Linalg, ()>;

#[rustfmt::skip]
fn rules() -> Vec<Rewrite<Linalg, ()>> { vec![
    rw!("mul-associativity1"; "(Mul (Mul ?x ?y) ?z)" => "(Mul ?x (Mul ?y ?z))"),
    rw!("mul-associativity2"; "(Mul ?x (Mul ?y ?z))" => "(Mul (Mul ?x ?y) ?z)"),
]}

fn chain(n: usize) -> String {
    let mut e = "(Mat 10 10)".to_string();
    for _ in 0..n {
        e = format!("(Mul {e} (Mat 10 10))");
    }
    e
}

fn main() {
    let mut instance = 20usize;
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
    egraph.add_expr(&chain(instance).parse().unwrap());
    egraph.rebuild();
    let t0 = Instant::now();
    let runner = Runner::default()
        .with_explanations_disabled()
        .with_scheduler(SimpleScheduler)
        .with_iter_limit(usize::MAX)
        .with_node_limit(usize::MAX)
        .with_time_limit(std::time::Duration::from_secs(u64::MAX / 4))
        .with_egraph(egraph)
        .run(&rules);
    let run_s = t0.elapsed().as_secs_f64();
    assert!(matches!(runner.stop_reason, Some(StopReason::Saturated)), "{:?}", runner.stop_reason);
    println!("iterations {}", runner.iterations.len());
    println!("run_s {run_s}");
    let mut sizes = std::collections::BTreeMap::<&str, usize>::new();
    for class in runner.egraph.classes() {
        for node in &class.nodes {
            // the i64 leaves of (Mat 10 10) are egg data nodes; egglog has no table for them
            let k = match node { Linalg::Mat(_) => "Mat", Linalg::Mul(_) => "Mul", Linalg::Num(_) => continue };
            *sizes.entry(k).or_default() += 1;
        }
    }
    println!("(");
    for (k, n) in &sizes { println!(" ({k} {n})"); }
    println!(")");
    eprintln!("egg: matmul-assoc-{instance}: {} iterations, {} e-classes, {run_s:.3}s",
        runner.iterations.len(), runner.egraph.number_of_classes());
    if let Some(p) = report {
        std::fs::write(p, format!("{{\"run_s\": {run_s}, \"iterations\": {}}}", runner.iterations.len())).unwrap();
    }
}
