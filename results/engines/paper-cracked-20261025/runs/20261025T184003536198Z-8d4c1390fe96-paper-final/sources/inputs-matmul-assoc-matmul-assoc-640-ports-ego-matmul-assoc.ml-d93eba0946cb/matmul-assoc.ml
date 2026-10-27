(* ego port of benchmarks/matmul-assoc/matmul-assoc-<n>.egg (from foresight's examples/mm):
   a chain of n multiplications of (Mat 10 10), the two associativity rules, run to
   saturation with ego's own loop. Mat is a nullary leaf here: its arguments are always
   10, 10, and egglog's (print-size) does not count the i64 literals either.

   Usage: matmul-assoc --instance N   (default 20)
   Built by systems/ego/build-ports.sh (a port is one OCaml file; see systems/ego/system.toml).
   stdout: "iterations <N>", "run_s <seconds>", then e-nodes per constructor in egglog
   (print-size) format.

   Saturation: BuildRunner(Sched).run_until_saturation with unbounded fuel and node limit and
   no [until] predicate calls apply_rules repeatedly until one call leaves eg.version
   unchanged, i.e. created no e-node and merged no two distinct classes (that is exactly
   egglog's saturation test), then asks Sched.should_stop, which says yes. One apply_rules
   call is one ego iteration: it snapshots the e-classes, then for each rule in order finds
   that rule's matches (canonicalising through the live union-find) and applies them before
   the next rule is searched, then rebuilds. The fixpoint is the same as egglog's; the number
   of iterations to reach it need not be. [iterations] is the number of apply_rules calls,
   the final no-change one included, as egglog counts its final no-op iteration. *)

open Ego.Generic

(* ** The language *)
module L = struct
  type 'a shape = Mat | Mul of 'a * 'a [@@deriving ord, show]
  type op = MatOp | MulOp [@@deriving eq]
  type t = Mk of t shape [@@unboxed]
  let op = function Mat -> MatOp | Mul _ -> MulOp
  let op_name = function MatOp -> "Mat" | MulOp -> "Mul"
  let children = function Mul (l, r) -> [l; r] | Mat -> []
  let map_children term f = match term with Mul (l, r) -> Mul (f l, f r) | Mat -> Mat
  let make op ls =
    match[@warning "-8"] op, ls with
    | MulOp, [l; r] -> Mul (l, r)
    | MatOp, [] -> Mat
end

(* ** No e-class analysis *)
module A = struct
  type t = unit
  type data = unit [@@deriving show, eq]
  let default = ()
end

module MA (S : GRAPH_API
           with type 'p t = (Ego.Id.t L.shape, A.t, A.data, 'p) egraph
            and type 'a shape := 'a L.shape
            and type analysis := A.t
            and type data := A.data
            and type node := L.t) = struct
  type 'p t = (Ego.Id.t L.shape, A.t, A.data, 'p) egraph
  let make : ro t -> Ego.Id.t L.shape -> A.data = fun _ _ -> ()
  let merge : A.t -> A.data -> A.data -> A.data * (bool * bool) = fun () () () -> ((), (false, false))
  let modify : 'a t -> Ego.Id.t -> unit = fun _ _ -> ()
end

module EGraph = Make (L) (A) (MA)

(* ** Rules (patterns as s-expressions, see Query.of_sexp: "?x" atoms are variables) *)
let op_of_string = function
  | "Mat" -> L.MatOp | "Mul" -> L.MulOp
  | s -> failwith ("unknown operator " ^ s)
let query = Query.of_sexp op_of_string

open Sexplib0.Sexp
let v x = Atom ("?" ^ x)
let mul a b = List [Atom "Mul"; a; b]
let x = v "x" and y = v "y" and z = v "z"

(* same order as matmul-assoc-<n>.egg *)
let rewrites : (Sexplib0.Sexp.t * Sexplib0.Sexp.t) list = [
  mul (mul x y) z, mul x (mul y z);                   (* mul-associativity1 *)
  mul x (mul y z), mul (mul x y) z;                   (* mul-associativity2 *)
]

(* ** Start term: Mul(...Mul(Mul(Mat, Mat), Mat)..., Mat) with n Muls, as gen.py builds it *)
let chain n : L.t =
  let mat = L.Mk L.Mat in
  let rec go e i = if i = 0 then e else go (L.Mk (L.Mul (e, mat))) (i - 1) in
  go mat n

(* ** Scheduler that never limits or bans anything (like ego's Scheduler.Simple), and counts
   the iterations and matches it is handed. apply_rules calls guard_rule_usage once per rule
   per iteration with the 0-based iteration index. *)
module Sched = struct
  type data = unit
  type t = { mutable iters : int; mutable matches : int; mutable t_iter : float }
  let default () = { iters = 0; matches = 0; t_iter = 0. }
  let create_rule_metadata _ _ = ()
  let should_stop _ _ _ = true
  let guard_rule_usage _graph t () iteration gen =
    if iteration + 1 > t.iters then begin
      let now = Unix.gettimeofday () in
      if t.iters > 0 then
        Printf.eprintf "ego: iteration %d: %d matches applied, %.3fs\n%!" t.iters t.matches (now -. t.t_iter);
      t.iters <- iteration + 1; t.matches <- 0; t.t_iter <- now
    end;
    Iter.map (fun m -> t.matches <- t.matches + 1; m) (gen ())
end

module Runner = EGraph.BuildRunner (Sched)

let () =
  let instance = ref 20 in
  let args = Array.to_list Sys.argv |> List.tl in
  let rec parse = function
    | "--instance" :: n :: rest -> instance := int_of_string n; parse rest
    | [] -> ()
    | a :: _ -> failwith ("unknown argument " ^ a) in
  parse args;

  let rules = List.map (fun (from, into) -> EGraph.Rule.make_constant ~from:(query from) ~into:(query into)) rewrites in
  let sched = Sched.default () in
  let graph = EGraph.init () in
  ignore (EGraph.add_node graph (chain !instance));
  EGraph.rebuild graph;

  let t0 = Unix.gettimeofday () in
  let saturated = Runner.run_until_saturation ~scheduler:sched ~node_limit:`Unbounded ~fuel:`Unbounded graph rules in
  let run_s = Unix.gettimeofday () -. t0 in
  assert saturated;
  let iterations = sched.iters in
  Printf.eprintf "ego: iteration %d: %d matches applied, %.3fs (no change: saturated)\n%!"
    iterations sched.matches (Unix.gettimeofday () -. sched.t_iter);

  (* e-nodes per constructor through the public API, distinct (class, canonical children)
     tuples because ego's hash-cons keeps stale keys; see horner.ml *)
  let fm = EGraph.find_matches (EGraph.freeze graph) in
  let count q =
    let seen = Hashtbl.create 1024 in
    fm (query q) |> Iter.iter (fun (eid, env) -> Hashtbl.replace seen (eid, StringMap.bindings env) ());
    Hashtbl.length seen in
  let classes = count (Atom "?x") in
  let counts = [ "Mat", count (Atom "Mat"); "Mul", count (mul (v "a0") (v "a1")) ] in
  let nodes = List.fold_left (fun k (_, c) -> k + c) 0 counts in
  Printf.printf "iterations %d\nrun_s %.9f\n(\n" iterations run_s;
  List.iter (fun (k, c) -> Printf.printf " (%s %d)\n" k c) counts;
  Printf.printf ")\n%!";
  Printf.eprintf "ego: matmul-assoc-%d: %d iterations, %d e-nodes, %d e-classes, run %.3fs\n%!"
    !instance iterations nodes classes run_s
