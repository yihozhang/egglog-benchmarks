(* ego port of benchmarks/horner/horner-<n>.egg (Horner's method, from foresight's examples/poly).
   Same language (Peano exponents), the same 11 rewrites in the same order, the polynomial of
   degree n built like gen.py does, run to saturation with ego's own loop.

   Usage: horner --instance N   (default 5)
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
  type 'a shape =
    | Zero
    | Succ of 'a
    | Var of string
    | Add of 'a * 'a
    | Mul of 'a * 'a
    | Pow of 'a * 'a
  [@@deriving ord, show]

  type op = ZeroOp | SuccOp | VarOp of string | AddOp | MulOp | PowOp [@@deriving eq]

  type t = Mk of t shape [@@unboxed]

  let op = function
    | Zero -> ZeroOp | Succ _ -> SuccOp | Var s -> VarOp s
    | Add _ -> AddOp | Mul _ -> MulOp | Pow _ -> PowOp

  let op_name = function
    | ZeroOp -> "Zero" | SuccOp -> "Succ" | VarOp _ -> "Var"
    | AddOp -> "Add" | MulOp -> "Mul" | PowOp -> "Pow"

  let children = function
    | Add (l, r) | Mul (l, r) | Pow (l, r) -> [l; r]
    | Succ l -> [l]
    | Zero | Var _ -> []

  let map_children term f = match term with
    | Add (l, r) -> Add (f l, f r) | Mul (l, r) -> Mul (f l, f r) | Pow (l, r) -> Pow (f l, f r)
    | Succ l -> Succ (f l)
    | Zero -> Zero | Var s -> Var s

  let make op ls =
    match[@warning "-8"] op, ls with
    | AddOp, [l; r] -> Add (l, r) | MulOp, [l; r] -> Mul (l, r) | PowOp, [l; r] -> Pow (l, r)
    | SuccOp, [l] -> Succ l
    | ZeroOp, [] -> Zero | VarOp s, [] -> Var s
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

(* ** Rules: patterns are built as s-expressions and read with Query.of_sexp (Query.t is
   abstract): atoms "?x" are pattern variables, "Zero" the nullary constructor, any other
   atom a Var. *)
let op_of_string = function
  | "Zero" -> L.ZeroOp | "Succ" -> L.SuccOp | "Add" -> L.AddOp | "Mul" -> L.MulOp | "Pow" -> L.PowOp
  | s -> L.VarOp s
let query = Query.of_sexp op_of_string

open Sexplib0.Sexp
let v x = Atom ("?" ^ x)
let add a b = List [Atom "Add"; a; b]
let mul a b = List [Atom "Mul"; a; b]
let pow a b = List [Atom "Pow"; a; b]
let succ a = List [Atom "Succ"; a]
let zero = Atom "Zero"
let x = v "x" and y = v "y" and z = v "z" and a = v "a" and b = v "b" and n = v "n"

(* same order as horner-<n>.egg *)
let rewrites : (Sexplib0.Sexp.t * Sexplib0.Sexp.t) list = [
  add x y, add y x;                                   (* add-commutativity *)
  mul x y, mul y x;                                   (* mul-commutativity *)
  add (add x y) z, add x (add y z);                   (* add-associativity1 *)
  add x (add y z), add (add x y) z;                   (* add-associativity2 *)
  mul (mul x y) z, mul x (mul y z);                   (* mul-associativity1 *)
  mul x (mul y z), mul (mul x y) z;                   (* mul-associativity2 *)
  mul x (add a b), add (mul x a) (mul x b);           (* distributivity1 *)
  add (mul x a) (mul x b), mul x (add a b);           (* distributivity2 *)
  mul x (succ zero), x;                               (* multiplicative-identity *)
  pow x zero, succ zero;                              (* power-null *)
  pow x (succ n), mul x (pow x n);                    (* power-reduction *)
]

(* ** Start term: ((a_n * x^n + a_{n-1} * x^{n-1}) + ...) + a_0, coefficients
   Var "a", "b", ... = a_0, a_1, ..., exponents Succ^i Zero, exactly as gen.py builds it. *)
let tadd l r = L.Mk (L.Add (l, r)) and tmul l r = L.Mk (L.Mul (l, r)) and tpow l r = L.Mk (L.Pow (l, r))
let tvar s = L.Mk (L.Var s)
let rec constant i = if i = 0 then L.Mk L.Zero else L.Mk (L.Succ (constant (i - 1)))
let coeff i = String.make 1 (Char.chr (Char.code 'a' + i))
let polynomial n : L.t =
  let vx = tvar "x" in
  let terms = List.init (n + 1) (fun i ->
      if i = 0 then tvar (coeff 0) else tmul (tvar (coeff i)) (tpow vx (constant i))) in
  match List.rev terms with
  | [] -> assert false
  | first :: rest -> List.fold_left tadd first rest

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
  let instance = ref 5 in
  let args = Array.to_list Sys.argv |> List.tl in
  let rec parse = function
    | "--instance" :: n :: rest -> instance := int_of_string n; parse rest
    | [] -> ()
    | a :: _ -> failwith ("unknown argument " ^ a) in
  parse args;

  let rules = List.map (fun (from, into) -> EGraph.Rule.make_constant ~from:(query from) ~into:(query into)) rewrites in
  let sched = Sched.default () in
  let graph = EGraph.init () in
  ignore (EGraph.add_node graph (polynomial !instance));
  EGraph.rebuild graph;

  let t0 = Unix.gettimeofday () in
  let saturated = Runner.run_until_saturation ~scheduler:sched ~node_limit:`Unbounded ~fuel:`Unbounded graph rules in
  let run_s = Unix.gettimeofday () -. t0 in
  assert saturated;
  let iterations = sched.iters in
  Printf.eprintf "ego: iteration %d: %d matches applied, %.3fs (no change: saturated)\n%!"
    iterations sched.matches (Unix.gettimeofday () -. sched.t_iter);

  (* e-nodes per constructor, counted through the public API: one e-class snapshot
     (find_matches partially applied), then the matches of the pattern (Op ?a0 ?a1) /
     (Op ?a0) / Op, one per e-class for ?x. The matcher canonicalises the bound ids, and
     distinct (class, bound ids) tuples are counted because ego's hash-cons keeps stale keys
     (a node whose children were re-canonicalised twice stays under its first key). Var
     leaves carry their name in the operator, so each name of the start term is probed. *)
  let fm = EGraph.find_matches (EGraph.freeze graph) in
  let count q =
    let seen = Hashtbl.create 1024 in
    fm (query q) |> Iter.iter (fun (eid, env) -> Hashtbl.replace seen (eid, StringMap.bindings env) ());
    Hashtbl.length seen in
  let classes = count (Atom "?x") in
  let vars = "x" :: List.init (!instance + 1) coeff in
  let counts = [
    "Add", count (add (v "a0") (v "a1"));
    "Mul", count (mul (v "a0") (v "a1"));
    "Pow", count (pow (v "a0") (v "a1"));
    "Succ", count (succ (v "a0"));
    "Var", List.fold_left (fun k s -> k + count (Atom s)) 0 vars;
    "Zero", count zero;
  ] in
  let nodes = List.fold_left (fun k (_, c) -> k + c) 0 counts in
  Printf.printf "iterations %d\nrun_s %.9f\n(\n" iterations run_s;
  List.iter (fun (k, c) -> Printf.printf " (%s %d)\n" k c) counts;
  Printf.printf ")\n%!";
  Printf.eprintf "ego: horner-%d: %d iterations, %d e-nodes, %d e-classes, run %.3fs\n%!"
    !instance iterations nodes classes run_s
