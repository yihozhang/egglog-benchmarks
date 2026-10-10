// foresight port of benchmarks/math-microbenchmark/math-microbenchmark.egg.
//
// Same language (i64 constants, string variables), the same 23 rewrites in the same order,
// the same seven start terms and the same number of iterations. Uses foresight's mutable
// hash-consed e-graph and MaximalRuleApplication: one iteration matches every rule against
// the current e-graph (matches are collected into a command schedule, the e-graph is not
// modified while matching), then applies all additions and unions; foresight's unionMany
// restores congruence inline, so the e-graph is canonical after every iteration. No match
// caching, no backoff/stochastic scheduling, no analyses, no extraction, no node/time limit,
// ParallelMap.sequential (single thread).
//
// Usage: bench.math_microbenchmark [--iters N] [--warmup N]
// Prints `run_s <seconds>` (the N iterations only) and the e-nodes per constructor in
// egglog's (print-size) format on stdout. --warmup N (default 6) first runs N iterations on
// a separate, discarded e-graph so that the JIT has compiled the engine before timing; 0
// disables it.
package bench

import foresight.eqsat.{EClassCall, MixedTree}
import foresight.eqsat.mutable.EGraph
import foresight.eqsat.parallel.ParallelMap
import foresight.eqsat.readonly
import foresight.eqsat.rewriting.Rule
import foresight.eqsat.rewriting.patterns.{Pattern, PatternMatch}
import foresight.eqsat.saturation.MaximalRuleApplication

sealed trait Math
object Math {
  case object Diff extends Math
  case object Integral extends Math
  case object Add extends Math
  case object Sub extends Math
  case object Mul extends Math
  case object Div extends Math
  case object Pow extends Math
  case object Ln extends Math
  case object Sqrt extends Math
  case object Sin extends Math
  case object Cos extends Math
  final case class Const(value: Long) extends Math
  final case class Var(name: String) extends Math

  /** egglog constructor name of a node type. */
  def name(op: Math): String = op match {
    case Diff => "Diff"; case Integral => "Integral"; case Add => "Add"; case Sub => "Sub"
    case Mul => "Mul"; case Div => "Div"; case Pow => "Pow"; case Ln => "Ln"; case Sqrt => "Sqrt"
    case Sin => "Sin"; case Cos => "Cos"; case Const(_) => "Const"; case Var(_) => "Var"
  }
}

object math_microbenchmark {
  import Math._

  type T[A] = MixedTree[Math, A]
  type P = T[Pattern.Var]
  type EG = readonly.EGraph[Math]
  type R = Rule[Math, PatternMatch[Math], EG]

  def n[A](op: Math, args: T[A]*): T[A] = MixedTree.Node(op, Seq.empty, Seq.empty, args)
  def c[A](v: Long): T[A] = n(Const(v))
  def sym[A](s: String): T[A] = n(Var(s))
  def pv(): P = MixedTree.Atom(Pattern.Var.fresh())

  def rw(name: String, lhs: P, rhs: P): R = Rule(name, lhs.toSearcher[EG], rhs.toApplier[EG])
  def r1(name: String)(f: P => (P, P)): R = { val (l, r) = f(pv()); rw(name, l, r) }
  def r2(name: String)(f: (P, P) => (P, P)): R = { val (l, r) = f(pv(), pv()); rw(name, l, r) }
  def r3(name: String)(f: (P, P, P) => (P, P)): R = { val (l, r) = f(pv(), pv(), pv()); rw(name, l, r) }

  /** The 23 rewrites of math-microbenchmark.egg, in the same order. */
  def rules: Seq[R] = Seq(
    r2("comm-add")  { (a, b) => n(Add, a, b) -> n(Add, b, a) },
    r2("comm-mul")  { (a, b) => n(Mul, a, b) -> n(Mul, b, a) },
    r3("assoc-add") { (a, b, c) => n(Add, a, n(Add, b, c)) -> n(Add, n(Add, a, b), c) },
    r3("assoc-mul") { (a, b, c) => n(Mul, a, n(Mul, b, c)) -> n(Mul, n(Mul, a, b), c) },

    r2("sub-canon") { (a, b) => n(Sub, a, b) -> n(Add, a, n(Mul, c(-1), b)) },

    r1("zero-add") { a => n(Add, a, c(0)) -> a },
    r1("zero-mul") { a => n(Mul, a, c(0)) -> c(0) },
    r1("one-mul")  { a => n(Mul, a, c(1)) -> a },

    r1("cancel-sub") { a => n(Sub, a, a) -> c(0) },

    r3("distribute") { (a, b, c) => n(Mul, a, n(Add, b, c)) -> n(Add, n(Mul, a, b), n(Mul, a, c)) },
    r3("factor")     { (a, b, c) => n(Add, n(Mul, a, b), n(Mul, a, c)) -> n(Mul, a, n(Add, b, c)) },

    r3("pow-mul") { (a, b, c) => n(Mul, n(Pow, a, b), n(Pow, a, c)) -> n(Pow, a, n(Add, b, c)) },
    r1("pow1") { x => n(Pow, x, c(1)) -> x },
    r1("pow2") { x => n(Pow, x, c(2)) -> n(Mul, x, x) },

    r3("d-add") { (x, a, b) => n(Diff, x, n(Add, a, b)) -> n(Add, n(Diff, x, a), n(Diff, x, b)) },
    r3("d-mul") { (x, a, b) =>
      n(Diff, x, n(Mul, a, b)) -> n(Add, n(Mul, a, n(Diff, x, b)), n(Mul, b, n(Diff, x, a))) },

    r1("d-sin") { x => n(Diff, x, n(Sin, x)) -> n(Cos, x) },
    r1("d-cos") { x => n(Diff, x, n(Cos, x)) -> n(Mul, c(-1), n(Sin, x)) },

    r1("i-one") { x => n(Integral, c(1), x) -> x },
    r1("i-cos") { x => n(Integral, n(Cos, x), x) -> n(Sin, x) },
    r1("i-sin") { x => n(Integral, n(Sin, x), x) -> n(Mul, c(-1), n(Cos, x)) },
    r3("i-sum") { (f, g, x) =>
      n(Integral, n(Add, f, g), x) -> n(Add, n(Integral, f, x), n(Integral, g, x)) },
    r3("i-dif") { (f, g, x) =>
      n(Integral, n(Sub, f, g), x) -> n(Sub, n(Integral, f, x), n(Integral, g, x)) },
    r3("i-parts") { (a, b, x) =>
      n(Integral, n(Mul, a, b), x) ->
        n(Sub, n(Mul, a, n(Integral, b, x)),
               n(Integral, n(Mul, n(Diff, x, a), n(Integral, b, x)), x)) },
  )

  /** The seven start terms of math-microbenchmark.egg. */
  def start: Seq[T[EClassCall]] = {
    def x = sym[EClassCall]("x")
    Seq(
      n(Integral, n(Ln, x), x),
      n(Integral, n(Add, x, n(Cos, x)), x),
      n(Integral, n(Mul, n(Cos, x), x), x),
      n(Diff, x, n(Add, c(1), n(Mul, c(2), x))),
      n(Diff, x, n(Sub, n(Pow, x, c(3)), n(Mul, c(7), n(Pow, x, c(2))))),
      n(Add, n(Mul, sym("y"), n(Add, x, sym("y"))), n(Sub, n(Add, x, c(2)), n(Add, x, x))),
      n(Div, c(1),
        n(Sub, n(Div, n(Add, c(1), n(Sqrt, sym("five"))), c(2)),
               n(Div, n(Sub, c(1), n(Sqrt, sym("five"))), c(2)))),
    )
  }

  def main(args: Array[String]): Unit = {
    var iters = 11
    var warmup = 6
    var i = 0
    while (i < args.length) {
      args(i) match {
        case "--iters" => iters = args(i + 1).toInt; i += 2
        case "--warmup" => warmup = args(i + 1).toInt; i += 2
        case a => throw new IllegalArgumentException(s"unknown argument $a")
      }
    }

    val pm = ParallelMap.sequential
    val rs = rules
    val step = MaximalRuleApplication.mutable[Math, EGraph[Math], PatternMatch[Math]](rs)

    def fresh(): EGraph[Math] = {
      val g = EGraph.empty[Math]
      start.foreach(g.add)
      g
    }
    def iterate(g: EGraph[Math], k: Int, log: Boolean): Unit = {
      var j = 0
      while (j < k) {
        val t = System.nanoTime()
        step.apply(g, (), pm)
        if (log) System.err.println(f"iteration ${j + 1}%2d: ${(System.nanoTime() - t) / 1e9}%.3f s")
        j += 1
      }
    }

    if (warmup > 0) {
      val g = fresh()
      iterate(g, warmup, log = false)
      System.gc()
    }

    val g = fresh()
    val t0 = System.nanoTime()
    iterate(g, iters, log = true)
    val runS = (System.nanoTime() - t0) / 1e9

    // e-nodes per constructor in the final e-graph: canonical classes, deduplicated nodes
    val counts = scala.collection.mutable.TreeMap.empty[String, Long]
    var classes = 0L
    var nodes = 0L
    for (ref <- g.classes) {
      val call = g.canonicalize(ref)
      require(call.ref == ref, s"non-canonical class $ref listed after rebuild")
      classes += 1
      for (nd <- g.nodes(call)) {
        nodes += 1
        val k = name(nd.nodeType)
        counts(k) = counts.getOrElse(k, 0L) + 1
      }
    }

    println(String.format(java.util.Locale.ROOT, "run_s %.6f", Double.box(runS)))
    println("(")
    for ((k, v) <- counts) println(s" ($k $v)")
    println(")")
    val gcs = java.lang.management.ManagementFactory.getGarbageCollectorMXBeans
    val gcNames = scala.jdk.CollectionConverters.ListHasAsScala(gcs).asScala.map(_.getName).mkString(", ")
    System.err.println(f"foresight: $iters iterations, $nodes e-nodes, $classes e-classes, $runS%.3f s" +
      f" (warm-up of $warmup iterations on a discarded e-graph excluded);" +
      s" JVM ${System.getProperty("java.version")}, GC [$gcNames]," +
      s" ${Runtime.getRuntime.availableProcessors} cpus, maxHeap ${Runtime.getRuntime.maxMemory >> 20} MiB")
  }
}
