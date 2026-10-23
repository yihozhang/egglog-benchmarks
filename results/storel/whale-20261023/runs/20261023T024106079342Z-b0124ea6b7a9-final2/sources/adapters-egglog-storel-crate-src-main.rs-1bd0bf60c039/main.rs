//! egglog-experimental's e-graph (for the back-off scheduler) plus the STOREL extension.
fn main() {
    let mut egraph = egglog_experimental::new_experimental_egraph();
    egglog_storel::register(&mut egraph);
    egglog::cli(egraph)
}
