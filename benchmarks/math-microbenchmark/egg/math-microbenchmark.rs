//! egg port of benchmarks/math-microbenchmark/math-microbenchmark.egg.
//!
//! Same language (i64 constants, string variables), the same 24 rewrites in the same
//! order, the same seven start terms and the same number of iterations. There is no
//! e-class analysis, so one egg iteration (search all rules, apply, rebuild) does the
//! same work as one egglog iteration, and the final e-graphs should be identical.
//! `SimpleScheduler` with no node or time limit mirrors egglog's `(run N)`.
//!
//! Usage: math-microbenchmark [--iters N] [--report FILE]
//! Prints e-nodes per constructor (egglog names, (print-size) format) on stdout and
//! writes run and phase times to the report file as JSON.

use egg::{rewrite as rw, *};
use std::time::Instant;

define_language! {
    pub enum Math {
        "Diff" = Diff([Id; 2]),
        "Integral" = Integral([Id; 2]),
        "Add" = Add([Id; 2]),
        "Sub" = Sub([Id; 2]),
        "Mul" = Mul([Id; 2]),
        "Div" = Div([Id; 2]),
        "Pow" = Pow([Id; 2]),
        "Ln" = Ln(Id),
        "Sqrt" = Sqrt(Id),
        "Sin" = Sin(Id),
        "Cos" = Cos(Id),
        // data variants: egg writes them as bare tokens, `1` and `x`, not 1 / (Var "x")
        Const(i64),
        Var(Symbol),
    }
}

type EGraph = egg::EGraph<Math, ()>;
type Rewrite = egg::Rewrite<Math, ()>;

#[rustfmt::skip]
fn rules() -> Vec<Rewrite> { vec![
    rw!("comm-add";  "(Add ?a ?b)"          => "(Add ?b ?a)"),
    rw!("comm-mul";  "(Mul ?a ?b)"          => "(Mul ?b ?a)"),
    rw!("assoc-add"; "(Add ?a (Add ?b ?c))" => "(Add (Add ?a ?b) ?c)"),
    rw!("assoc-mul"; "(Mul ?a (Mul ?b ?c))" => "(Mul (Mul ?a ?b) ?c)"),

    rw!("sub-canon"; "(Sub ?a ?b)" => "(Add ?a (Mul -1 ?b))"),

    rw!("zero-add"; "(Add ?a 0)" => "?a"),
    rw!("zero-mul"; "(Mul ?a 0)" => "0"),
    rw!("one-mul";  "(Mul ?a 1)" => "?a"),

    rw!("cancel-sub"; "(Sub ?a ?a)" => "0"),

    rw!("distribute"; "(Mul ?a (Add ?b ?c))"          => "(Add (Mul ?a ?b) (Mul ?a ?c))"),
    rw!("factor";     "(Add (Mul ?a ?b) (Mul ?a ?c))" => "(Mul ?a (Add ?b ?c))"),

    rw!("pow-mul"; "(Mul (Pow ?a ?b) (Pow ?a ?c))" => "(Pow ?a (Add ?b ?c))"),
    rw!("pow1"; "(Pow ?x 1)" => "?x"),
    rw!("pow2"; "(Pow ?x 2)" => "(Mul ?x ?x)"),

    rw!("d-add"; "(Diff ?x (Add ?a ?b))" => "(Add (Diff ?x ?a) (Diff ?x ?b))"),
    rw!("d-mul"; "(Diff ?x (Mul ?a ?b))" => "(Add (Mul ?a (Diff ?x ?b)) (Mul ?b (Diff ?x ?a)))"),

    rw!("d-sin"; "(Diff ?x (Sin ?x))" => "(Cos ?x)"),
    rw!("d-cos"; "(Diff ?x (Cos ?x))" => "(Mul -1 (Sin ?x))"),

    rw!("i-one"; "(Integral 1 ?x)" => "?x"),
    rw!("i-cos"; "(Integral (Cos ?x) ?x)" => "(Sin ?x)"),
    rw!("i-sin"; "(Integral (Sin ?x) ?x)" => "(Mul -1 (Cos ?x))"),
    rw!("i-sum"; "(Integral (Add ?f ?g) ?x)" => "(Add (Integral ?f ?x) (Integral ?g ?x))"),
    rw!("i-dif"; "(Integral (Sub ?f ?g) ?x)" => "(Sub (Integral ?f ?x) (Integral ?g ?x))"),
    rw!("i-parts"; "(Integral (Mul ?a ?b) ?x)" =>
        "(Sub (Mul ?a (Integral ?b ?x)) (Integral (Mul (Diff ?x ?a) (Integral ?b ?x)) ?x))"),
]}

/// The seven start terms of math-microbenchmark.egg in egg's syntax (constants and
/// variables are bare tokens).
const START: &[&str] = &[
    "(Integral (Ln x) x)",
    "(Integral (Add x (Cos x)) x)",
    "(Integral (Mul (Cos x) x) x)",
    "(Diff x (Add 1 (Mul 2 x)))",
    "(Diff x (Sub (Pow x 3) (Mul 7 (Pow x 2))))",
    "(Add (Mul y (Add x y)) (Sub (Add x 2) (Add x x)))",
    "(Div 1 (Sub (Div (Add 1 (Sqrt five)) 2) (Div (Sub 1 (Sqrt five)) 2)))",
];

fn main() {
    let mut iters = 11usize;
    let mut report = None;
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--iters" => iters = args.next().expect("--iters N").parse().expect("--iters N"),
            "--report" => report = Some(args.next().expect("--report FILE")),
            _ => panic!("unknown argument {a}"),
        }
    }

    let rules = rules();
    let mut egraph = EGraph::default();
    for s in START {
        let expr: RecExpr<Math> = s.parse().unwrap();
        egraph.add_expr(&expr);
    }
    egraph.rebuild();

    let t0 = Instant::now();
    let runner = Runner::default()
        .with_explanations_disabled()
        .with_scheduler(SimpleScheduler)
        .with_iter_limit(iters)
        .with_node_limit(usize::MAX)
        .with_time_limit(std::time::Duration::from_secs(u64::MAX / 4))
        .with_egraph(egraph)
        .run(&rules);
    let run_s = t0.elapsed().as_secs_f64();

    let mut sizes = std::collections::BTreeMap::<&str, usize>::new();
    for class in runner.egraph.classes() {
        for node in &class.nodes {
            let name = match node {
                Math::Diff(_) => "Diff", Math::Integral(_) => "Integral", Math::Add(_) => "Add",
                Math::Sub(_) => "Sub", Math::Mul(_) => "Mul", Math::Div(_) => "Div",
                Math::Pow(_) => "Pow", Math::Ln(_) => "Ln", Math::Sqrt(_) => "Sqrt",
                Math::Sin(_) => "Sin", Math::Cos(_) => "Cos", Math::Const(_) => "Const",
                Math::Var(_) => "Var",
            };
            *sizes.entry(name).or_default() += 1;
        }
    }
    println!("(");
    for (name, n) in &sizes {
        println!(" ({name} {n})");
    }
    println!(")");
    eprintln!(
        "egg: {} iterations, {} e-nodes, {} e-classes, stop reason {:?}, {:.3}s",
        runner.iterations.len(), runner.egraph.total_number_of_nodes(),
        runner.egraph.number_of_classes(), runner.stop_reason, run_s
    );

    if let Some(path) = report {
        let search: f64 = runner.iterations.iter().map(|i| i.search_time).sum();
        let apply: f64 = runner.iterations.iter().map(|i| i.apply_time).sum();
        let rebuild: f64 = runner.iterations.iter().map(|i| i.rebuild_time).sum();
        std::fs::write(path, format!(
            "{{\"run_s\": {run_s}, \"search_apply_s\": {}, \"merge_s\": 0, \"rebuild_s\": {rebuild}, \"iterations\": {}, \"nodes\": {}, \"classes\": {}}}",
            search + apply, runner.iterations.len(),
            runner.egraph.total_number_of_nodes(), runner.egraph.number_of_classes()
        )).expect("write report");
    }
}
