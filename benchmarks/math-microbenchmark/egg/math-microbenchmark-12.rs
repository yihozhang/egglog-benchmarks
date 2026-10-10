//! math-microbenchmark with (run 12): the same port with a different default iteration count.
#[path = "math-microbenchmark.rs"]
#[allow(dead_code)]
mod base;

fn main() {
    base::main_with(12)
}
