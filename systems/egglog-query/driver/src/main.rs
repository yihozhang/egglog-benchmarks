//! Query-only benchmark driver for egglog (see systems/egglog-query/system.toml).
//!
//! export <program.egg> <outdir>
//!     Run the program to its final state, then write <outdir>/schema.json and one CSV per
//!     table (<outdir>/tables/<name>.csv) for the SQL baselines. Nothing is timed.
//! run <program.egg> [-j N] <test.egg>...
//!     Run the program to its final state (setup, not timed), then run every test file (a
//!     relation R, a ruleset and one :naive rule writing R, then `(run <ruleset> 1)`) in turn.
//!     Prints one JSON line per test: wall_s of the whole test command (parse, type check,
//!     rule compilation, query, rebuild), query_s = egglog's search+apply + merge time of the
//!     test ruleset, rebuild_s, other_s = wall_s - query_s - rebuild_s, and |R|.
use egglog::{CommandOutput, Core, EGraph, Read};
use egglog_numeric_id::NumericId;
use std::io::Write;
use std::time::Instant;

fn run_program(path: &str, threads: usize) -> EGraph {
    let mut egraph = EGraph::default();
    egraph.set_num_threads(threads);
    // only affects rules added after the program (the tests): the program itself runs as usual
    let no_decomp = std::env::var("EGGLOG_QUERY_NO_DECOMP").is_ok();
    let text = std::fs::read_to_string(path).expect("read program");
    egraph
        .parse_and_run_program(Some(path.to_string()), &text)
        .expect("program failed");
    egraph.no_decomp = no_decomp;
    egraph
}

fn csv_field(s: &str) -> String {
    if s.contains([',', '"', '\n']) {
        format!("\"{}\"", s.replace('"', "\"\""))
    } else {
        s.to_string()
    }
}

fn export(program: &str, outdir: &str) {
    let egraph = run_program(program, 1);
    let tables_dir = format!("{outdir}/tables");
    std::fs::create_dir_all(&tables_dir).unwrap();
    let mut schema = Vec::new();
    egraph.read(|rs| {
        let names: Vec<String> = rs.tables().map(|s| s.to_string()).collect();
        for name in names {
            if rs.is_table_hidden(&name) == Some(true) {
                continue;
            }
            let ctor = rs.constructor_schema(&name).ok();
            let ft = ctor.or_else(|| rs.function_schema(&name).ok());
            let Some(ft) = ft else { continue };
            let inputs: Vec<String> = ft.input.iter().map(|s| s.name().to_string()).collect();
            let output = ft.output.name().to_string();
            let kind = if ctor.is_some() { "constructor" } else { "function" };
            let enc = |v: egglog::Value, sort: &str| -> String {
                match sort {
                    "i64" => rs.value_to_base::<i64>(v).to_string(),
                    "f64" => rs.value_to_base::<egglog::sort::F>(v).0.0.to_string(),
                    "String" => csv_field(&rs.value_to_base::<egglog::sort::S>(v).0),
                    "bool" => rs.value_to_base::<bool>(v).to_string(),
                    _ => v.rep().to_string(), // e-class id (or container id)
                }
            };
            let mut out = std::io::BufWriter::new(
                std::fs::File::create(format!("{tables_dir}/{name}.csv")).unwrap(),
            );
            let mut rows = 0usize;
            let mut subsumed_rows = 0usize;
            let mut write_row = |vals: &[egglog::Value], o: egglog::Value, subsumed: bool| {
                let mut fields: Vec<String> =
                    vals.iter().zip(&inputs).map(|(v, s)| enc(*v, s)).collect();
                if output != "Unit" {
                    fields.push(enc(o, &output));
                }
                fields.push(subsumed.to_string());
                writeln!(out, "{}", fields.join(",")).unwrap();
                rows += 1;
                subsumed_rows += subsumed as usize;
            };
            if ctor.is_some() {
                rs.constructor_enodes(&name, |e| write_row(e.children, e.eclass, e.subsumed))
                    .unwrap();
            } else {
                rs.function_entries(&name, |e| write_row(e.inputs, e.output, e.subsumed))
                    .unwrap();
            }
            schema.push(serde_json::json!({
                "name": name, "kind": kind, "inputs": inputs, "output": output,
                "rows": rows, "subsumed_rows": subsumed_rows,
            }));
        }
    });
    std::fs::write(
        format!("{outdir}/schema.json"),
        serde_json::to_string_pretty(&schema).unwrap(),
    )
    .unwrap();
    eprintln!("exported {} tables to {outdir}", schema.len());
}

fn run_tests(program: &str, threads: usize, tests: &[String]) {
    let mut egraph = run_program(program, threads);
    // EGGLOG_QUERY_PPROF=<path>: sample (pprof, ~1 kHz) only while the tests run, then write
    // <path>.svg (flamegraph) and <path>.txt (sampled stacks); program setup is not sampled.
    let pprof_path = std::env::var("EGGLOG_QUERY_PPROF").ok();
    let guard = pprof_path.as_ref().map(|_| {
        pprof::ProfilerGuardBuilder::default().frequency(997)
            .blocklist(&["libc", "libgcc", "pthread", "vdso"]).build().expect("pprof")
    });
    for test in tests {
        let text = std::fs::read_to_string(test).expect("read test");
        // the test file declares (relation <R> ...) and (ruleset <rs>) on its first lines
        // the result table is the first declared relation or function
        let rel = ["(function ", "(relation "]
            .iter()
            .find_map(|d| text.split(d).nth(1))
            .and_then(|s| s.split_whitespace().next())
            .unwrap()
            .to_string();
        let ruleset = text.split("(ruleset ").nth(1).and_then(|s| s.split(')').next()).unwrap().trim().to_string();
        let t0 = Instant::now();
        let outputs = match egraph.parse_and_run_program(Some(test.clone()), &text) {
            Ok(o) => o,
            Err(e) => {
                println!("{}", serde_json::json!({"test": test, "error": e.to_string()}));
                continue;
            }
        };
        let wall = t0.elapsed().as_secs_f64();
        let mut query = 0.0;
        let mut rebuild = 0.0;
        let mut per_run = Vec::new();
        for o in &outputs {
            if let CommandOutput::RunSchedule(r) = o {
                macro_rules! get {
                    ($m:expr) => {
                        $m.get(ruleset.as_str()).map(|d| d.as_secs_f64()).unwrap_or(0.0)
                    };
                }
                let (sa, mg) = (get!(r.search_and_apply_time_per_ruleset), get!(r.merge_time_per_ruleset));
                let q = sa + mg;
                per_run.push((sa, mg));
                query += q;
                rebuild += get!(r.rebuild_time_per_ruleset);
            }
        }
        let size = egraph.read(|rs| rs.table_size(&rel)).unwrap_or(0);
        println!(
            "{}",
            serde_json::json!({"test": test, "relation": rel, "ruleset": ruleset, "threads": threads,
                "wall_s": wall, "query_s": query, "rebuild_s": rebuild,
                "other_s": wall - query - rebuild, "size": size, "query_per_run_s": per_run})
        );
    }
    if let (Some(path), Some(guard)) = (pprof_path, guard) {
        write_profile(&path, guard);
    }
}

fn write_profile(path: &str, guard: pprof::ProfilerGuard<'_>) {
    let report = guard.report().build().expect("pprof report");
    report.flamegraph(std::fs::File::create(format!("{path}.svg")).unwrap()).unwrap();
    std::fs::write(format!("{path}.txt"), format!("{report:?}")).unwrap();
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("export") => export(&args[1], &args[2]),
        Some("run") => {
            let mut threads = 1;
            let mut rest = Vec::new();
            let mut it = args[2..].iter();
            while let Some(a) = it.next() {
                if a == "-j" {
                    threads = it.next().unwrap().parse().unwrap();
                } else {
                    rest.push(a.clone());
                }
            }
            run_tests(&args[1], threads, &rest)
        }
        _ => {
            eprintln!("usage: driver export <program> <outdir> | driver run <program> [-j N] <test.egg>...");
            std::process::exit(2)
        }
    }
}
