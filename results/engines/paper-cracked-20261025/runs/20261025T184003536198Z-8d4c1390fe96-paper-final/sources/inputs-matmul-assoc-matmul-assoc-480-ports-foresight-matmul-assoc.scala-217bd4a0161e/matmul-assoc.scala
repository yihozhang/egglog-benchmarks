// foresight port of benchmarks/matmul-assoc/matmul-assoc-<n>.egg (matrix-chain associativity;
// foresight CC'26, 7.2).
//
// Built on foresight's own example code: the language foresight.eqsat.examples.mm.LinalgExpr
// (Mat(rows, cols), Mul) and the two associativity rules examples.mm.Rules().all, which the
// image publishes from the repo's `examples` module. The chain Mul(...Mul(Mul(Mat, Mat), Mat)...)
// of n multiplications of 10x10 matrices is built exactly as gen.py and the repo's
// MatmulBenchmarks.createNmm do, and saturated with the paper's strategy
// MaximalRuleApplication.mutable(rules).repeatUntilStable on the plain mutable hash-consed
// e-graph (no analyses). One iteration = match every rule against the current e-graph, apply
// all additions and unions (congruence restored inline). repeatUntilStable stops after an
// iteration that adds no e-node and merges no e-classes (egglog's criterion); that final
// iteration is counted. No extraction, no match caching, no node/time limits.
//
// Usage: bench.matmul_assoc --instance N [--warmup M] [--threads T]
//   --warmup M   JIT warm-up: first saturate a chain of M (default 10; 0 disables) on a discarded e-graph
//   --threads T  T > 1: ParallelMap.fixedThreadParallel(T) for matching and application; else sequential
// Prints `wall_s` (after the warm-up: start e-graph + saturation + size count), `iterations`,
// `run_s` (the saturation only) and the e-nodes per constructor in egglog's (print-size)
// format on stdout. Mat's two Int fields live in the node payload (one Mat e-node, no literal
// leaves), matching egglog, which has no table for i64 literals.
package bench

import foresight.eqsat.{EClassCall, ENode, MixedTree}
import foresight.eqsat.examples.mm.{LinalgExpr, LinalgIR, Mat, Mul, Rules}
import foresight.eqsat.lang.Language
import foresight.eqsat.mutable
import foresight.eqsat.parallel.ParallelMap
import foresight.eqsat.saturation.{MaximalRuleApplication, Strategy}

object matmul_assoc {
  type EG = mutable.EGraph[LinalgIR]

  val L: Language[LinalgExpr] = summon[Language[LinalgExpr]]
  val R: Rules = Rules()(using L)

  /** Mul(...Mul(Mul(Mat(10,10), Mat(10,10)), Mat(10,10))..., Mat(10,10)) with n Muls. */
  def chain(n: Int): LinalgExpr = {
    var e: LinalgExpr = Mat(10, 10)
    var i = 0
    while (i < n) { e = Mul(e, Mat(10, 10)); i += 1 }
    e
  }

  /** egglog constructor name of an e-node, obtained by decoding it back to the surface language. */
  def name(nd: ENode[LinalgIR]): String = L.fromTree[EClassCall](MixedTree.fromENode(nd)) match {
    case Mat(_, _) => "Mat"
    case Mul(_, _) => "Mul"
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

  def fresh(n: Int): EG = L.toMutableEGraph(chain(n))._2

  /** Saturate; returns the number of iterations (including the final no-change one). */
  def saturate(g: EG, pm: ParallelMap, log: Boolean): Int = {
    val step = new Step(log)
    step.repeatUntilStable(g, pm)
    step.iterations
  }

  def main(args: Array[String]): Unit = {
    var instance = -1
    var warmup = 10
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
    require(instance >= 1, "--instance N (number of multiplications) is required")
    require(threads >= 1, s"--threads must be >= 1, got $threads")

    val pm = if (threads == 1) ParallelMap.sequential else ParallelMap.fixedThreadParallel(threads)

    if (warmup > 0) {
      saturate(fresh(warmup), pm, log = false)
      System.gc()
    }

    // wall_s: everything after the JIT warm-up, measured here: building the start e-graph,
    // the saturation and the size count (VM startup excluded).
    val tWall = System.nanoTime()
    val g = fresh(instance)
    val t0 = System.nanoTime()
    val iterations = saturate(g, pm, log = true)
    val runS = (System.nanoTime() - t0) / 1e9

    // e-nodes per constructor in the final e-graph: canonical classes, deduplicated nodes
    val counts = scala.collection.mutable.TreeMap.empty[String, Long]
    var classes = 0L
    var nodes = 0L
    for (ref <- g.classes) {
      val call = g.canonicalize(ref)
      require(call.ref == ref, s"non-canonical class $ref listed after saturation")
      classes += 1
      for (nd <- g.nodes(call)) {
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
    System.err.println(f"foresight: matmul-assoc-$instance: $iterations iterations, $nodes e-nodes, $classes e-classes," +
      f" $runS%.3f s (warm-up on a chain of $warmup excluded); threads $threads;" +
      s" JVM ${System.getProperty("java.version")}, GC [$gcNames]," +
      s" ${Runtime.getRuntime.availableProcessors} cpus, maxHeap ${Runtime.getRuntime.maxMemory >> 20} MiB")
  }
}
