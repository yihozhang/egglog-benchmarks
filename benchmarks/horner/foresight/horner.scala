// foresight port of benchmarks/horner/horner-<n>.egg (Horner's method; foresight CC'26, 7.2).
//
// Built on foresight's own example code: the language foresight.eqsat.examples.poly.ArithExpr
// (Zero, Succ, Var, Add, Mul, Pow) and the 11 rules examples.poly.Rules().all, which the image
// publishes from the repo's `examples` module. The polynomial of degree n is built exactly as
// gen.py and the repo's PolyBenchmarks.polynomialExpr do, and saturated with the paper's
// strategy MaximalRuleApplication.mutable(rules).repeatUntilStable on the mutable hash-consed
// e-graph (wrapped in an EGraphWithMetadata with no analyses, because the example rules are
// typed against it). One iteration = match every rule against the current e-graph, apply all
// additions and unions (congruence restored inline). repeatUntilStable stops after an
// iteration that adds no e-node and merges no e-classes (egglog's criterion); that final
// iteration is counted. No extraction, no match caching, no node/time limits.
//
// Usage: bench.horner --instance N [--warmup M] [--threads T]
//   --warmup M   JIT warm-up: first saturate degree M (default 4; 0 disables) on a discarded e-graph
//   --threads T  T > 1: ParallelMap.fixedThreadParallel(T) for matching and application; else sequential
// Prints `wall_s` (after the warm-up: start e-graph + saturation + size count), `iterations`,
// `run_s` (the saturation only) and the e-nodes per constructor in egglog's (print-size)
// format on stdout. Var's String field lives in the node payload (one Var e-node per name, no
// literal leaves), matching egglog, which has no table for String literals.
package bench

import foresight.eqsat.{EClassCall, ENode, MixedTree}
import foresight.eqsat.examples.poly.{Add, ArithExpr, ArithIR, Mul, Pow, Rules, Succ, Var, Zero}
import foresight.eqsat.lang.Language
import foresight.eqsat.mutable
import foresight.eqsat.parallel.ParallelMap
import foresight.eqsat.saturation.{MaximalRuleApplication, Strategy}

object horner {
  type EG = mutable.EGraphWithMetadata[ArithIR, mutable.EGraph[ArithIR]]

  val L: Language[ArithExpr] = summon[Language[ArithExpr]]
  val R: Rules = Rules()(using L)

  /** Peano numeral. */
  def const(i: Int): ArithExpr = if (i == 0) Zero else Succ(const(i - 1))

  /** ((a_n * x^n + a_{n-1} * x^{n-1}) + ...) + a_0 with a_0 = Var("a"), a_1 = Var("b"), ... */
  def polynomial(n: Int): ArithExpr = {
    val x = Var("x")
    val coeffs = (0 to n).map(i => Var(('a' + i).toChar.toString))
    val terms = coeffs.zipWithIndex.reverse.map { case (c, i) => if (i == 0) c else Mul(c, Pow(x, const(i))) }
    terms.reduce(Add(_, _))
  }

  /** egglog constructor name of an e-node, obtained by decoding it back to the surface language. */
  def name(nd: ENode[ArithIR]): String = L.fromTree[EClassCall](MixedTree.fromENode(nd)) match {
    case Zero => "Zero"
    case Succ(_) => "Succ"
    case Var(_) => "Var"
    case Add(_, _) => "Add"
    case Mul(_, _) => "Mul"
    case Pow(_, _) => "Pow"
    case other => throw new IllegalStateException(s"unexpected e-node $other")
  }

  /** The paper's step, wrapped so that iterations are counted and (optionally) logged. */
  final class Step(log: Boolean) extends Strategy[EG, Unit] {
    private val step: Strategy[EG, Unit] = MaximalRuleApplication.mutable(R.all)
    var iterations = 0
    override def initialData: Unit = ()
    override def apply(g: EG, data: Unit, pm: ParallelMap): (Option[EG], Unit) = {
      iterations += 1
      val t = System.nanoTime()
      val r = step(g, data, pm)
      if (log) System.err.println(f"iteration $iterations%2d: ${(System.nanoTime() - t) / 1e9}%.3f s" +
        (if (r._1.isEmpty) " (no change)" else ""))
      r
    }
  }

  def fresh(n: Int): (EG, mutable.EGraph[ArithIR]) = {
    val (_, eg) = L.toMutableEGraph(polynomial(n))
    (mutable.EGraphWithMetadata[ArithIR, mutable.EGraph[ArithIR]](eg), eg)
  }

  /** Saturate; returns the number of iterations (including the final no-change one). */
  def saturate(g: EG, pm: ParallelMap, log: Boolean): Int = {
    val step = new Step(log)
    step.repeatUntilStable(g, pm)
    step.iterations
  }

  def main(args: Array[String]): Unit = {
    var instance = -1
    var warmup = 4
    var threads = 1
    var i = 0
    while (i < args.length) {
      args(i) match {
        case "--instance" => instance = args(i + 1).toInt; i += 2
        case "--warmup" => warmup = args(i + 1).toInt; i += 2
        case "--threads" => threads = args(i + 1).toInt; i += 2
        case a => throw new IllegalArgumentException(s"unknown argument $a")
      }
    }
    require(instance >= 1, "--instance N (degree of the polynomial) is required")
    require(threads >= 1, s"--threads must be >= 1, got $threads")

    val pm = if (threads == 1) ParallelMap.sequential else ParallelMap.fixedThreadParallel(threads)

    if (warmup > 0) {
      val (g, _) = fresh(warmup)
      saturate(g, pm, log = false)
      System.gc()
    }

    // wall_s: everything after the JIT warm-up, measured here: building the start e-graph,
    // the saturation and the size count (VM startup excluded).
    val tWall = System.nanoTime()
    val (g, eg) = fresh(instance)
    val t0 = System.nanoTime()
    val iterations = saturate(g, pm, log = true)
    val runS = (System.nanoTime() - t0) / 1e9

    // e-nodes per constructor in the final e-graph: canonical classes, deduplicated nodes
    val counts = scala.collection.mutable.TreeMap.empty[String, Long]
    var classes = 0L
    var nodes = 0L
    for (ref <- eg.classes) {
      val call = eg.canonicalize(ref)
      require(call.ref == ref, s"non-canonical class $ref listed after saturation")
      classes += 1
      for (nd <- eg.nodes(call)) {
        nodes += 1
        val k = name(nd)
        counts(k) = counts.getOrElse(k, 0L) + 1
      }
    }

    val wallS = (System.nanoTime() - tWall) / 1e9
    println(String.format(java.util.Locale.ROOT, "wall_s %.6f", Double.box(wallS)))
    println(s"iterations $iterations")
    println(String.format(java.util.Locale.ROOT, "run_s %.6f", Double.box(runS)))
    println("(")
    for ((k, v) <- counts) println(s" ($k $v)")
    println(")")
    val gcs = java.lang.management.ManagementFactory.getGarbageCollectorMXBeans
    val gcNames = scala.jdk.CollectionConverters.ListHasAsScala(gcs).asScala.map(_.getName).mkString(", ")
    System.err.println(f"foresight: horner-$instance: $iterations iterations, $nodes e-nodes, $classes e-classes," +
      f" $runS%.3f s (warm-up on degree $warmup excluded); threads $threads;" +
      s" JVM ${System.getProperty("java.version")}, GC [$gcNames]," +
      s" ${Runtime.getRuntime.availableProcessors} cpus, maxHeap ${Runtime.getRuntime.maxMemory >> 20} MiB")
  }
}
