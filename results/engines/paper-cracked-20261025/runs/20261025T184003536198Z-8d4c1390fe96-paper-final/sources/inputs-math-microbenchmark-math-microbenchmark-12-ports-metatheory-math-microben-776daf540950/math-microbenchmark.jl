# Metatheory.jl port of benchmarks/math-microbenchmark/math-microbenchmark.egg.
#
# Same language, the same 23 rewrites in the same order, the same seven start terms and
# the same number of iterations. The constructors are symbol heads of ordinary call terms
# (`Add(a, b)`, `Const(1)`, `Var("x")`), deliberately not Julia's `+`, `*`, ... so that no
# built-in simplification, binarization or constant folding can happen; `Const` and `Var`
# wrap Julia literals, so a `Const` e-node is a term e-node whose child class holds the
# literal, like in egglog.
#
# One Metatheory iteration (eqsat_step!) = e-match every rule against the current e-graph,
# instantiate every match and merge, rebuild. `SaturationParams(timeout = N)` is an
# iteration limit; `SimpleScheduler` applies every rule every time; the class limit is
# disabled (0), the node limit is unused by this version. There is no goal and no
# extraction, so this is egglog's `(run N)`.
#
# Usage: julia --project=<project with Metatheory> math-microbenchmark.jl [--iters N] [--warmup N]
# Prints `wall_s` (after the warm-up: start e-graph + saturate! + size count),
# `run_s` (time of the single saturate! call, after a warm-up saturate! on a separate
# e-graph so JIT compilation is excluded) and the e-nodes per constructor in egglog's
# (print-size) format on stdout; everything else goes to stderr.

module MathMicrobenchmark

using Metatheory: @theory, @rule, EGraph, addexpr!, rebuild!, saturate!, SaturationParams,
    ENodeTerm, ENodeLiteral, find, operation
using Metatheory.EGraphs.Schedulers: SimpleScheduler

const HEADS = (:Diff, :Integral, :Add, :Sub, :Mul, :Div, :Pow, :Ln, :Sqrt, :Sin, :Cos, :Const, :Var)
# The @theory macro turns a head into a symbol only when nothing of that name is in scope
# (otherwise the function object is stored and matched by name); make sure the heads are
# plain symbols so e-nodes are compared symbol-to-symbol with no fallback.
for h in HEADS
    isdefined(@__MODULE__, h) && error("constructor name $h is bound in the port module")
end

const RULES = @theory begin
    Add(~a, ~b) --> Add(~b, ~a)
    Mul(~a, ~b) --> Mul(~b, ~a)
    Add(~a, Add(~b, ~c)) --> Add(Add(~a, ~b), ~c)
    Mul(~a, Mul(~b, ~c)) --> Mul(Mul(~a, ~b), ~c)

    Sub(~a, ~b) --> Add(~a, Mul(Const(-1), ~b))

    Add(~a, Const(0)) --> ~a
    Mul(~a, Const(0)) --> Const(0)
    Mul(~a, Const(1)) --> ~a

    Sub(~a, ~a) --> Const(0)

    Mul(~a, Add(~b, ~c)) --> Add(Mul(~a, ~b), Mul(~a, ~c))
    Add(Mul(~a, ~b), Mul(~a, ~c)) --> Mul(~a, Add(~b, ~c))

    Mul(Pow(~a, ~b), Pow(~a, ~c)) --> Pow(~a, Add(~b, ~c))
    Pow(~x, Const(1)) --> ~x
    Pow(~x, Const(2)) --> Mul(~x, ~x)

    Diff(~x, Add(~a, ~b)) --> Add(Diff(~x, ~a), Diff(~x, ~b))
    Diff(~x, Mul(~a, ~b)) --> Add(Mul(~a, Diff(~x, ~b)), Mul(~b, Diff(~x, ~a)))

    Diff(~x, Sin(~x)) --> Cos(~x)
    Diff(~x, Cos(~x)) --> Mul(Const(-1), Sin(~x))

    Integral(Const(1), ~x) --> ~x
    Integral(Cos(~x), ~x) --> Sin(~x)
    Integral(Sin(~x), ~x) --> Mul(Const(-1), Cos(~x))
    Integral(Add(~f, ~g), ~x) --> Add(Integral(~f, ~x), Integral(~g, ~x))
    Integral(Sub(~f, ~g), ~x) --> Sub(Integral(~f, ~x), Integral(~g, ~x))
    Integral(Mul(~a, ~b), ~x) -->
        Sub(Mul(~a, Integral(~b, ~x)), Integral(Mul(Diff(~x, ~a), Integral(~b, ~x)), ~x))
end

# The seven start terms of math-microbenchmark.egg.
const START = [
    :(Integral(Ln(Var("x")), Var("x"))),
    :(Integral(Add(Var("x"), Cos(Var("x"))), Var("x"))),
    :(Integral(Mul(Cos(Var("x")), Var("x")), Var("x"))),
    :(Diff(Var("x"), Add(Const(1), Mul(Const(2), Var("x"))))),
    :(Diff(Var("x"), Sub(Pow(Var("x"), Const(3)), Mul(Const(7), Pow(Var("x"), Const(2)))))),
    :(Add(Mul(Var("y"), Add(Var("x"), Var("y"))), Sub(Add(Var("x"), Const(2)), Add(Var("x"), Var("x"))))),
    :(Div(Const(1), Sub(Div(Add(Const(1), Sqrt(Var("five"))), Const(2)),
                        Div(Sub(Const(1), Sqrt(Var("five"))), Const(2))))),
]

function params(iters)
    SaturationParams(
        timeout = iters,            # iteration limit
        timelimit = UInt64(0),      # no time limit
        eclasslimit = 0,            # 0 disables the e-class limit
        enodelimit = typemax(Int),  # unused by this version, but unlimited anyway
        goal = nothing,
        scheduler = SimpleScheduler,
        timer = true,               # per-phase TimerOutputs, printed on stderr
    )
end

function start_egraph()
    g = EGraph()
    for t in START
        addexpr!(g, t)
    end
    rebuild!(g)
    g
end

# E-nodes per constructor in the final e-graph. Metatheory's rebuild canonicalizes e-nodes
# in place but only deduplicates the node lists of classes it repaired, and its hashcons
# keeps stale entries, so count canonical (head, canonical children) pairs ourselves.
function sizes(g)
    seen = Set{Tuple{Symbol,Vector{Int}}}()
    literals = Set{Any}()
    counts = Dict{Symbol,Int}(h => 0 for h in HEADS)
    for (_, class) in g.classes
        for n in class.nodes
            if n isa ENodeTerm
                op = operation(n)
                op isa Symbol && op in HEADS || error("unexpected e-node head $(repr(op))")
                key = (op, Int[find(g, a) for a in n.args])
                key in seen && continue
                push!(seen, key)
                counts[op] += 1
            else
                push!(literals, n.value)
            end
        end
    end
    counts, length(seen), length(literals)
end

function main(args)
    iters, warmup = 11, 3
    i = 1
    while i <= length(args)
        if args[i] == "--iters"
            iters = parse(Int, args[i + 1]); i += 2
        elseif args[i] == "--warmup"
            warmup = parse(Int, args[i + 1]); i += 2
        else
            error("unknown argument $(args[i])")
        end
    end

    # Warm-up on a separate e-graph: the same rules and start terms for a few iterations,
    # so that the rule matchers, saturate! and rebuild! are compiled before timing.
    t0 = time_ns()
    if warmup > 0
        gw = start_egraph()
        rw = saturate!(gw, RULES, params(warmup))
        sizes(gw)
        println(stderr, "metatheory: warm-up $(rw.iterations) iterations on a separate e-graph ",
                "($(length(gw.classes)) classes), $(round((time_ns() - t0) / 1e9, digits = 3))s ",
                "of JIT compilation excluded from run_s")
    end

    # wall_s: everything after the JIT warm-up, measured here: building the start e-graph,
    # the iterations and the size count (Julia startup and package load excluded).
    twall = time_ns()
    g = start_egraph()
    compile0 = Base.cumulative_compile_time_ns()
    gc0 = Base.gc_time_ns()
    t1 = time_ns()
    report = saturate!(g, RULES, params(iters))
    run_s = (time_ns() - t1) / 1e9
    gc_s = (Base.gc_time_ns() - gc0) / 1e9
    compile1 = Base.cumulative_compile_time_ns()
    compile_s = (compile1[1] - compile0[1]) / 1e9

    counts, nodes, nliterals = sizes(g)
    wall_s = (time_ns() - twall) / 1e9
    println("wall_s ", wall_s)
    println("run_s ", run_s)
    println("(")
    for h in sort(collect(HEADS), by = string)
        println(" (", h, " ", counts[h], ")")
    end
    println(")")

    println(stderr, "metatheory: $(report.iterations) iterations, stop reason $(report.reason), ",
            "$nodes constructor e-nodes + $nliterals literal e-nodes, $(length(g.classes)) e-classes ",
            "(literals are their own classes), $(round(run_s, digits = 3))s ",
            "(gc $(round(gc_s, digits = 3))s, compilation inside the timed region $(round(compile_s, digits = 3))s)")
    show(stderr, report)
    println(stderr)
end

end # module

MathMicrobenchmark.main(ARGS)
