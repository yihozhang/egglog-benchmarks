# Metatheory.jl port of benchmarks/horner/horner-<n>.egg (Horner's method, from foresight's
# examples/poly). Same language (Peano exponents), the same 11 rewrites in the same order,
# the polynomial of degree n built like gen.py does, run to saturation.
#
# The constructors are symbol heads of ordinary call terms (`Add(a, b)`, `Succ(Zero())`,
# `Var("x")`), deliberately not Julia's `+`, `*`, `^`, so that no built-in simplification,
# binarization or constant folding can happen; `Zero()` is a nullary term e-node and `Var`
# wraps a Julia string literal, whose e-class is a literal e-class (not counted below).
#
# Saturation: `saturate!` with `SimpleScheduler` (every rule every iteration), no class/node/
# time limit, no goal, no extraction, and an iteration limit far above what is needed. One
# iteration (eqsat_step!) = e-match all rules, instantiate every match and merge, rebuild.
# Metatheory stops with reason `:saturated` after an iteration whose applications performed
# no merge (`isempty(g.dirty)`); since a new e-node is a new singleton class merged with the
# matched class, that is exactly an iteration that added no e-node and merged nothing, and
# it is counted in `report.iterations`, like egglog's final no-change run of
# `(run-schedule (saturate (run)))`.
#
# Usage: julia --project=<project with Metatheory> horner.jl --instance N [--warmup K]
# Prints `wall_s` (after the JIT warm-up: start e-graph + saturate! + size count),
# `iterations`, `run_s` (the saturate! call only, reference) and the e-nodes per
# constructor in egglog's (print-size) format on stdout; everything else goes to stderr.
# The warm-up saturates a smaller instance (degree K, default min(N, 4)) on a separate
# e-graph so that the matchers, saturate!, rebuild! and the size count are compiled first.

module Horner

using Metatheory: @theory, @rule, EGraph, addexpr!, rebuild!, saturate!, SaturationParams,
    ENodeTerm, ENodeLiteral, find, operation
using Metatheory.EGraphs.Schedulers: SimpleScheduler

const HEADS = (:Zero, :Succ, :Var, :Add, :Mul, :Pow)
# The @theory macro turns a head into a symbol only when nothing of that name is in scope
# (otherwise the function object is stored and matched by name); make sure the heads are
# plain symbols so e-nodes are compared symbol-to-symbol with no fallback.
for h in HEADS
    isdefined(@__MODULE__, h) && error("constructor name $h is bound in the port module")
end

const RULES = @theory begin
    Add(~x, ~y) --> Add(~y, ~x)                                   # add-commutativity
    Mul(~x, ~y) --> Mul(~y, ~x)                                   # mul-commutativity
    Add(Add(~x, ~y), ~z) --> Add(~x, Add(~y, ~z))                 # add-associativity1
    Add(~x, Add(~y, ~z)) --> Add(Add(~x, ~y), ~z)                 # add-associativity2
    Mul(Mul(~x, ~y), ~z) --> Mul(~x, Mul(~y, ~z))                 # mul-associativity1
    Mul(~x, Mul(~y, ~z)) --> Mul(Mul(~x, ~y), ~z)                 # mul-associativity2
    Mul(~x, Add(~a, ~b)) --> Add(Mul(~x, ~a), Mul(~x, ~b))        # distributivity1
    Add(Mul(~x, ~a), Mul(~x, ~b)) --> Mul(~x, Add(~a, ~b))        # distributivity2
    Mul(~x, Succ(Zero())) --> ~x                                  # multiplicative-identity
    Pow(~x, Zero()) --> Succ(Zero())                              # power-null
    Pow(~x, Succ(~n)) --> Mul(~x, Pow(~x, ~n))                    # power-reduction
end

# Peano numeral i: Succ^i(Zero).
peano(i) = i == 0 ? :(Zero()) : :(Succ($(peano(i - 1))))

# ((a_n x^n + a_{n-1} x^{n-1}) + ...) + a_0, coefficients Var("a"), Var("b"), ... = a_0, a_1, ...
function polynomial(n)
    x = :(Var("x"))
    coeff(i) = :(Var($(string(Char('a' + i)))))
    terms = [i == 0 ? coeff(0) : :(Mul($(coeff(i)), Pow($x, $(peano(i))))) for i in 0:n]
    reverse!(terms)  # a_n x^n first, like foresight's zipWithIndex.reverse
    acc = terms[1]
    for t in terms[2:end]
        acc = :(Add($acc, $t))
    end
    acc
end

function params()
    SaturationParams(
        timeout = 100_000,          # iteration limit, never reached: we stop at :saturated
        timelimit = UInt64(0),      # no time limit
        eclasslimit = 0,            # 0 disables the e-class limit
        enodelimit = typemax(Int),  # unused by this version, but unlimited anyway
        goal = nothing,
        scheduler = SimpleScheduler,
        timer = true,               # per-phase TimerOutputs, printed on stderr
    )
end

function start_egraph(n)
    g = EGraph()
    addexpr!(g, polynomial(n))
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
    instance, warmup = 5, nothing
    i = 1
    while i <= length(args)
        if args[i] == "--instance"
            instance = parse(Int, args[i + 1]); i += 2
        elseif args[i] == "--warmup"
            warmup = parse(Int, args[i + 1]); i += 2
        else
            error("unknown argument $(args[i])")
        end
    end
    warmup === nothing && (warmup = min(instance, 4))

    # Warm-up on a separate e-graph: the same rules on a smaller polynomial, saturated to
    # completion, so that the rule matchers, saturate!, rebuild! and sizes are compiled
    # before timing.
    t0 = time_ns()
    if warmup > 0
        gw = start_egraph(warmup)
        rw = saturate!(gw, RULES, params())
        sizes(gw)
        println(stderr, "metatheory: warm-up horner-$warmup on a separate e-graph: $(rw.iterations) ",
                "iterations, stop reason $(rw.reason), $(length(gw.classes)) classes, ",
                "$(round((time_ns() - t0) / 1e9, digits = 3))s of JIT compilation excluded from wall_s")
    end

    # wall_s: everything after the JIT warm-up, measured here: building the start e-graph,
    # the iterations and the size count (Julia startup and package load excluded).
    twall = time_ns()
    g = start_egraph(instance)
    compile0 = Base.cumulative_compile_time_ns()
    gc0 = Base.gc_time_ns()
    t1 = time_ns()
    report = saturate!(g, RULES, params())
    run_s = (time_ns() - t1) / 1e9
    gc_s = (Base.gc_time_ns() - gc0) / 1e9
    compile1 = Base.cumulative_compile_time_ns()
    compile_s = (compile1[1] - compile0[1]) / 1e9

    counts, nodes, nliterals = sizes(g)
    wall_s = (time_ns() - twall) / 1e9
    report.reason === :saturated ||
        error("metatheory stopped after $(report.iterations) iterations with reason $(report.reason), not :saturated")
    println("wall_s ", wall_s)
    println("iterations ", report.iterations)
    println("run_s ", run_s)
    println("(")
    for h in sort(collect(HEADS), by = string)
        println(" (", h, " ", counts[h], ")")
    end
    println(")")

    println(stderr, "metatheory: horner-$instance: $(report.iterations) iterations, stop reason $(report.reason), ",
            "$nodes constructor e-nodes + $nliterals literal e-nodes, $(length(g.classes)) e-classes ",
            "(literals are their own classes), $(round(run_s, digits = 3))s ",
            "(gc $(round(gc_s, digits = 3))s, compilation inside the timed region $(round(compile_s, digits = 3))s)")
    show(stderr, report)
    println(stderr)
end

end # module

Horner.main(ARGS)
