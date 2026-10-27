(* ego port of benchmarks/math-microbenchmark/math-microbenchmark.egg.
   Same language (int constants, string variables), the same 24 rewrites in the same
   order, the same seven start terms and the same number of iterations.

   Usage: math-microbenchmark [--iters N]   (default 11)
   Built by systems/ego/build-ports.sh (a port is one OCaml file; see systems/ego/system.toml).
   stdout: "run_s <seconds>" then e-nodes per constructor in egglog (print-size) format.

   One iteration must be: match all rules on the current e-graph, apply all matches,
   rebuild. ego's own Generic.apply_rules interleaves these (rule 0's matches are applied
   before rule 1 is searched, and matching consults the live union-find), so the iteration
   is driven through the public BuildRunner(SCHEDULER) hook instead: see [Sched] below. *)

open Ego.Generic

(* ** The language *)
module L = struct
  type 'a shape =
    | Diff of 'a * 'a
    | Integral of 'a * 'a
    | Add of 'a * 'a
    | Sub of 'a * 'a
    | Mul of 'a * 'a
    | Div of 'a * 'a
    | Pow of 'a * 'a
    | Ln of 'a
    | Sqrt of 'a
    | Sin of 'a
    | Cos of 'a
    | Const of int
    | Var of string
  [@@deriving ord, show]

  type op =
    | DiffOp | IntegralOp | AddOp | SubOp | MulOp | DivOp | PowOp
    | LnOp | SqrtOp | SinOp | CosOp | ConstOp of int | VarOp of string
  [@@deriving eq]

  type t = Mk of t shape [@@unboxed]

  let op = function
    | Diff _ -> DiffOp | Integral _ -> IntegralOp | Add _ -> AddOp | Sub _ -> SubOp
    | Mul _ -> MulOp | Div _ -> DivOp | Pow _ -> PowOp | Ln _ -> LnOp | Sqrt _ -> SqrtOp
    | Sin _ -> SinOp | Cos _ -> CosOp | Const c -> ConstOp c | Var s -> VarOp s

  let op_name = function
    | DiffOp -> "Diff" | IntegralOp -> "Integral" | AddOp -> "Add" | SubOp -> "Sub"
    | MulOp -> "Mul" | DivOp -> "Div" | PowOp -> "Pow" | LnOp -> "Ln" | SqrtOp -> "Sqrt"
    | SinOp -> "Sin" | CosOp -> "Cos" | ConstOp _ -> "Const" | VarOp _ -> "Var"

  let children = function
    | Diff (l, r) | Integral (l, r) | Add (l, r) | Sub (l, r) | Mul (l, r)
    | Div (l, r) | Pow (l, r) -> [l; r]
    | Ln l | Sqrt l | Sin l | Cos l -> [l]
    | Const _ | Var _ -> []

  let map_children term f = match term with
    | Diff (l, r) -> Diff (f l, f r) | Integral (l, r) -> Integral (f l, f r)
    | Add (l, r) -> Add (f l, f r) | Sub (l, r) -> Sub (f l, f r)
    | Mul (l, r) -> Mul (f l, f r) | Div (l, r) -> Div (f l, f r)
    | Pow (l, r) -> Pow (f l, f r) | Ln l -> Ln (f l) | Sqrt l -> Sqrt (f l)
    | Sin l -> Sin (f l) | Cos l -> Cos (f l)
    | Const c -> Const c | Var s -> Var s

  let make op ls =
    match[@warning "-8"] op, ls with
    | DiffOp, [l; r] -> Diff (l, r) | IntegralOp, [l; r] -> Integral (l, r)
    | AddOp, [l; r] -> Add (l, r) | SubOp, [l; r] -> Sub (l, r)
    | MulOp, [l; r] -> Mul (l, r) | DivOp, [l; r] -> Div (l, r)
    | PowOp, [l; r] -> Pow (l, r) | LnOp, [l] -> Ln l | SqrtOp, [l] -> Sqrt l
    | SinOp, [l] -> Sin l | CosOp, [l] -> Cos l
    | ConstOp c, [] -> Const c | VarOp s, [] -> Var s
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
   abstract): atoms "?x" are pattern variables, integer atoms constants, other atoms Vars. *)
let op_of_string = function
  | "Diff" -> L.DiffOp | "Integral" -> L.IntegralOp | "Add" -> L.AddOp | "Sub" -> L.SubOp
  | "Mul" -> L.MulOp | "Div" -> L.DivOp | "Pow" -> L.PowOp | "Ln" -> L.LnOp | "Sqrt" -> L.SqrtOp
  | "Sin" -> L.SinOp | "Cos" -> L.CosOp
  | s -> (match int_of_string_opt s with Some n -> L.ConstOp n | None -> L.VarOp s)
let query = Query.of_sexp op_of_string

open Sexplib0.Sexp
let v x = Atom ("?" ^ x)
let c n = Atom (string_of_int n)
let bin op a b = List [Atom op; a; b]
let un op a = List [Atom op; a]
let add = bin "Add" and sub = bin "Sub" and mul = bin "Mul" and pow = bin "Pow"
let diff = bin "Diff" and integral = bin "Integral"
let sin = un "Sin" and cos = un "Cos"

let a = v "a" and b = v "b" and cc = v "c" and x = v "x" and f = v "f" and g = v "g"

(* same order as math-microbenchmark.egg (the Div rule there is commented out) *)
let rewrites : (Sexplib0.Sexp.t * Sexplib0.Sexp.t) list = [
  add a b, add b a;                                          (* comm-add *)
  mul a b, mul b a;                                          (* comm-mul *)
  add a (add b cc), add (add a b) cc;                        (* assoc-add *)
  mul a (mul b cc), mul (mul a b) cc;                        (* assoc-mul *)

  sub a b, add a (mul (c (-1)) b);                           (* sub-canon *)

  add a (c 0), a;                                            (* zero-add *)
  mul a (c 0), c 0;                                          (* zero-mul *)
  mul a (c 1), a;                                            (* one-mul *)

  sub a a, c 0;                                              (* cancel-sub *)

  mul a (add b cc), add (mul a b) (mul a cc);                (* distribute *)
  add (mul a b) (mul a cc), mul a (add b cc);                (* factor *)

  mul (pow a b) (pow a cc), pow a (add b cc);                (* pow-mul *)
  pow x (c 1), x;                                            (* pow1 *)
  pow x (c 2), mul x x;                                      (* pow2 *)

  diff x (add a b), add (diff x a) (diff x b);               (* d-add *)
  diff x (mul a b), add (mul a (diff x b)) (mul b (diff x a)); (* d-mul *)

  diff x (sin x), cos x;                                     (* d-sin *)
  diff x (cos x), mul (c (-1)) (sin x);                      (* d-cos *)

  integral (c 1) x, x;                                       (* i-one *)
  integral (cos x) x, sin x;                                 (* i-cos *)
  integral (sin x) x, mul (c (-1)) (cos x);                  (* i-sin *)
  integral (add f g) x, add (integral f x) (integral g x);   (* i-sum *)
  integral (sub f g) x, sub (integral f x) (integral g x);   (* i-dif *)
  integral (mul a b) x,                                      (* i-parts *)
    sub (mul a (integral b x)) (integral (mul (diff x a) (integral b x)) x);
]

(* ** Start terms *)
let t2 k l r = L.Mk (k (l, r)) and t1 k l = L.Mk (k l)
let tadd = t2 (fun (l, r) -> L.Add (l, r)) and tsub = t2 (fun (l, r) -> L.Sub (l, r))
let tmul = t2 (fun (l, r) -> L.Mul (l, r)) and tdiv = t2 (fun (l, r) -> L.Div (l, r))
let tpow = t2 (fun (l, r) -> L.Pow (l, r)) and tdiff = t2 (fun (l, r) -> L.Diff (l, r))
let tintegral = t2 (fun (l, r) -> L.Integral (l, r))
let tln = t1 (fun l -> L.Ln l) and tsqrt = t1 (fun l -> L.Sqrt l) and tcos = t1 (fun l -> L.Cos l)
let tconst n = L.Mk (L.Const n) and tvar s = L.Mk (L.Var s)
let vx = tvar "x" and vy = tvar "y" and five = tvar "five"

let start : L.t list = [
  tintegral (tln vx) vx;
  tintegral (tadd vx (tcos vx)) vx;
  tintegral (tmul (tcos vx) vx) vx;
  tdiff vx (tadd (tconst 1) (tmul (tconst 2) vx));
  tdiff vx (tsub (tpow vx (tconst 3)) (tmul (tconst 7) (tpow vx (tconst 2))));
  tadd (tmul vy (tadd vx vy)) (tsub (tadd vx (tconst 2)) (tadd vx vx));
  tdiv (tconst 1) (tsub (tdiv (tadd (tconst 1) (tsqrt five)) (tconst 2))
                        (tdiv (tsub (tconst 1) (tsqrt five)) (tconst 2)));
]

(* ** Iteration = search all, then apply all, then rebuild.

   BuildRunner's apply_rules computes one snapshot of the e-classes, then for each rule in
   order asks the scheduler's guard_rule_usage for that rule's matches (handing it a
   generator over the shared snapshot) and immediately applies them; finally it rebuilds.
   Only run_until_saturation is public; called with unbounded fuel and node limit and
   ~until:(fun _ -> true) it performs exactly one apply_rules (it stops because the version
   changed, or else because should_stop says so). [Sched] runs it twice per iteration:
   - collect phase: stores every rule's generator and returns no matches; on the last rule
     it materialises all rules' matches, so every match is found on the e-graph as it was
     at the start of the iteration (nothing has been merged yet). The trailing rebuild of
     this call is a no-op (nothing is pending).
   - apply phase: returns the stored matches rule by rule; ego instantiates the right-hand
     sides, unions, and rebuilds. *)
type matches = (Ego.Id.t * Ego.Id.t StringMap.t) array

module Sched = struct
  type data = int (* rule index *)
  type t = {
    n : int;
    gens : (unit -> (Ego.Id.t * Ego.Id.t StringMap.t) Iter.t) array;
    cache : matches array;
    mutable collect : bool;
    mutable n_matches : int;
    mutable next : int; (* run_until_saturation asks for every rule's metadata, in order *)
  }
  let make n = { n; gens = Array.make n (fun () -> Iter.empty); cache = Array.make n [||];
                 collect = true; n_matches = 0; next = 0 }
  let default () = failwith "Sched.default: use Sched.make"
  let create_rule_metadata t _ = let i = t.next in t.next <- (i + 1) mod t.n; i
  let should_stop _ _ _ = true
  let guard_rule_usage _graph t idx _iteration gen =
    if t.collect then begin
      t.gens.(idx) <- gen;
      if idx = t.n - 1 then
        for i = 0 to t.n - 1 do
          let m = Iter.to_array (t.gens.(i) ()) in
          t.n_matches <- t.n_matches + Array.length m;
          t.cache.(i) <- m
        done;
      Iter.empty
    end else begin
      let m = t.cache.(idx) in
      t.cache.(idx) <- [||];
      Iter.of_array m
    end
end

module Runner = EGraph.BuildRunner (Sched)

let () =
  let iters = ref 11 in
  let args = Array.to_list Sys.argv |> List.tl in
  let rec parse = function
    | "--iters" :: n :: rest -> iters := int_of_string n; parse rest
    | [] -> ()
    | a :: _ -> failwith ("unknown argument " ^ a) in
  parse args;

  let rules = List.map (fun (from, into) -> EGraph.Rule.make_constant ~from:(query from) ~into:(query into)) rewrites in
  let sched = Sched.make (List.length rules) in
  let graph = EGraph.init () in
  List.iter (fun t -> ignore (EGraph.add_node graph t)) start;
  EGraph.rebuild graph;

  let search_s = ref 0. and apply_s = ref 0. in
  let t0 = Unix.gettimeofday () in
  for i = 0 to !iters - 1 do
    let ta = Unix.gettimeofday () in
    let step collect =
      sched.collect <- collect; sched.next <- 0;
      ignore (Runner.run_until_saturation ~scheduler:sched ~node_limit:`Unbounded ~fuel:`Unbounded
                ~until:(fun _ -> true) graph rules) in
    step true;
    let tb = Unix.gettimeofday () in
    step false;
    let tc = Unix.gettimeofday () in
    search_s := !search_s +. (tb -. ta);
    apply_s := !apply_s +. (tc -. tb);
    Printf.eprintf "ego: iteration %d: %d matches, search %.3fs, apply+rebuild %.3fs\n%!"
      (i + 1) sched.n_matches (tb -. ta) (tc -. tb);
    sched.n_matches <- 0
  done;
  let run_s = Unix.gettimeofday () -. t0 in

  (* e-nodes per constructor, counted through the public API: one e-class snapshot
     (find_matches partially applied), then the matches of the pattern (Op ?a ?b) / (Op ?a),
     one per e-class for ?x. The matcher canonicalises the bound ids, and distinct
     (class, bound ids) tuples are counted because ego's hash-cons keeps stale keys: a pending
     entry carries the node as created, so once (Add a b) has been re-canonicalised to
     (Add a' b) and a' is merged again, (Add a' b) is never removed (it never shadows a
     canonical lookup, so the equivalences are unaffected). Leaves carry their value in the
     operator, so every literal that can ever exist (the rules copy literals; there is no
     constant folding) is probed individually: the integer / symbol atoms of the start terms
     and rules. *)
  let fm = EGraph.find_matches (EGraph.freeze graph) in
  let count q =
    let seen = Hashtbl.create 1024 in
    fm (query q) |> Iter.iter (fun (eid, env) -> Hashtbl.replace seen (eid, StringMap.bindings env) ());
    Hashtbl.length seen in
  let classes = count (Atom "?x") in
  let counts = ref [] in
  List.iter (fun (op, arity) ->
      let args = List.init arity (fun i -> Atom (Printf.sprintf "?a%d" i)) in
      counts := (op, count (List (Atom op :: args))) :: !counts)
    ["Diff", 2; "Integral", 2; "Add", 2; "Sub", 2; "Mul", 2; "Div", 2; "Pow", 2;
     "Ln", 1; "Sqrt", 1; "Sin", 1; "Cos", 1];
  let rec atoms_of_sexp acc = function
    | Atom s -> if String.length s > 0 && s.[0] = '?' then acc else s :: acc
    | List l -> List.fold_left atoms_of_sexp acc l in
  let rec atoms_of_term acc (L.Mk n) = match n with
    | L.Const i -> string_of_int i :: acc
    | L.Var s -> s :: acc
    | n -> List.fold_left atoms_of_term acc (L.children n) in
  let literals =
    List.fold_left atoms_of_term [] start
    @ List.fold_left (fun acc (l, r) -> atoms_of_sexp (atoms_of_sexp acc l) r) [] rewrites
    |> List.filter (fun s -> match op_of_string s with L.ConstOp _ | L.VarOp _ -> true | _ -> false)
    |> List.sort_uniq Stdlib.compare in
  let consts, vars = List.partition (fun s -> int_of_string_opt s <> None) literals in
  let leaves names = List.fold_left (fun n s -> n + count (Atom s)) 0 names in
  counts := ("Const", leaves consts) :: ("Var", leaves vars) :: !counts;
  let nodes = List.fold_left (fun n (_, k) -> n + k) 0 !counts in
  Printf.printf "run_s %.9f\n(\n" run_s;
  List.sort Stdlib.compare !counts |> List.iter (fun (k, n) -> Printf.printf " (%s %d)\n" k n);
  Printf.printf ")\n%!";
  Printf.eprintf "ego: %d iterations, %d e-nodes, %d e-classes, search %.3fs, apply+rebuild %.3fs, run %.3fs\n%!"
    !iters nodes classes !search_s !apply_s run_s;
  (* overhead of forcing search-all-then-apply-all through the public API, at the final size:
     one extra e-class snapshot and one extra no-op rebuild per iteration *)
  let t = Unix.gettimeofday () in
  let _snapshot = EGraph.find_matches (EGraph.freeze graph) in
  let snap = Unix.gettimeofday () -. t in
  let t = Unix.gettimeofday () in
  EGraph.rebuild graph;
  let rb = Unix.gettimeofday () -. t in
  Printf.eprintf "ego: overhead estimate per iteration at final size: snapshot %.3fs + idle rebuild %.3fs\n%!" snap rb
