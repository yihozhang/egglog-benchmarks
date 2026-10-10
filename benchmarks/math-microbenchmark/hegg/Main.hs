{-# LANGUAGE BangPatterns #-}
{-# LANGUAGE DeriveTraversable #-}
{-# LANGUAGE LambdaCase #-}
{-# LANGUAGE MonoLocalBinds #-}
-- | hegg port of benchmarks/math-microbenchmark/math-microbenchmark.egg.
--
-- Same language (i64 constants, string variables), the same 23 rewrites in the same
-- order, the same seven start terms and the same number of iterations. The analysis is
-- the trivial @()@ analysis, there is no scheduler, no node/time limit and no extraction.
--
-- hegg's own driver ('Data.Equality.Saturation.runEqualitySaturation') cannot be used
-- as is: it stops at saturation or after a hard-coded 30 iterations and threads a
-- 'Scheduler' through every rule. So the loop is written here from hegg's building blocks,
-- doing per iteration exactly what that driver does: build the relational database from
-- the e-graph, 'ematch' every rule against it, apply every match (represent the
-- right-hand side and 'merge'), then 'rebuild' once. The e-graph is a persistent value,
-- so every match of an iteration is found in the e-graph as it was at the start of it.
--
-- Usage: math-microbenchmark [--iters N]
-- Prints @run_s <seconds>@ and the e-nodes per constructor (egglog names, (print-size)
-- format) on stdout; details on stderr.
module Main (main) where

import Control.Exception (evaluate)
import Control.Monad.Trans.State.Strict (State, runState, state)
import Data.Foldable (foldl')
import Data.Int (Int64)
import qualified Data.IntMap.Strict as IM
import qualified Data.IntSet as IS
import qualified Data.Map.Strict as M
import qualified Data.Set as S
import GHC.Clock (getMonotonicTime)
import System.Environment (getArgs)
import System.IO (hPutStrLn, stderr)
import Text.Printf (hPrintf, printf)

import Data.Equality.Graph (ClassId, ENode (..), add, canonicalize, children, emptyEGraph, find,
                            merge, operator, rebuild, represent)
import Data.Equality.Graph.Classes (EClass (..))
import Data.Equality.Graph.Internal (EGraph (..))
import Data.Equality.Graph.Nodes (NodeMap (..))
import Data.Equality.Matching (Match (..), Pattern (..), ematch)
import Data.Equality.Matching.Database (Database (..), IntTrie (..), Subst)
import Data.Equality.Saturation.Rewrites (Rewrite (..))
import Data.Equality.Utils (Fix (..))
import Data.Equality.Utils.SizedList (toListSL)

-- * The language: egglog's Math datatype in base-functor form

data Math a
  = Diff a a
  | Integral a a
  | Add a a
  | Sub a a
  | Mul a a
  | Div a a
  | Pow a a
  | Ln a
  | Sqrt a
  | Sin a
  | Cos a
  | Const !Int64
  | Var !String
  deriving (Eq, Ord, Show, Functor, Foldable, Traversable)

-- | egglog constructor name of an e-node.
ctor :: Math a -> String
ctor = \case
  Diff{} -> "Diff"; Integral{} -> "Integral"; Add{} -> "Add"; Sub{} -> "Sub"
  Mul{} -> "Mul"; Div{} -> "Div"; Pow{} -> "Pow"; Ln{} -> "Ln"; Sqrt{} -> "Sqrt"
  Sin{} -> "Sin"; Cos{} -> "Cos"; Const{} -> "Const"; Var{} -> "Var"

type G = EGraph () Math

-- * The 23 rewrites

type P = Pattern Math

-- Pattern variables are ints; hegg numbers the internal variables of a compiled query
-- from 0, so user variables must not collide with small numbers.
a, b, c, x, f, g :: P
a = VariablePattern 101
b = VariablePattern 102
c = VariablePattern 103
x = VariablePattern 104
f = VariablePattern 105
g = VariablePattern 106

k :: Int64 -> P
k n = NonVariablePattern (Const n)

add', sub', mul', pow', diff', integral' :: P -> P -> P
add' p q = NonVariablePattern (Add p q)
sub' p q = NonVariablePattern (Sub p q)
mul' p q = NonVariablePattern (Mul p q)
pow' p q = NonVariablePattern (Pow p q)
diff' p q = NonVariablePattern (Diff p q)
integral' p q = NonVariablePattern (Integral p q)

sin', cos' :: P -> P
sin' p = NonVariablePattern (Sin p)
cos' p = NonVariablePattern (Cos p)

rules :: [Rewrite () Math]
rules =
  [ add' a b := add' b a                                     -- comm-add
  , mul' a b := mul' b a                                     -- comm-mul
  , add' a (add' b c) := add' (add' a b) c                   -- assoc-add
  , mul' a (mul' b c) := mul' (mul' a b) c                   -- assoc-mul

  , sub' a b := add' a (mul' (k (-1)) b)                     -- sub-canon

  , add' a (k 0) := a                                        -- zero-add
  , mul' a (k 0) := k 0                                      -- zero-mul
  , mul' a (k 1) := a                                        -- one-mul

  , sub' a a := k 0                                          -- cancel-sub

  , mul' a (add' b c) := add' (mul' a b) (mul' a c)          -- distribute
  , add' (mul' a b) (mul' a c) := mul' a (add' b c)          -- factor

  , mul' (pow' a b) (pow' a c) := pow' a (add' b c)          -- pow-mul
  , pow' x (k 1) := x                                        -- pow1
  , pow' x (k 2) := mul' x x                                 -- pow2

  , diff' x (add' a b) := add' (diff' x a) (diff' x b)       -- d-add
  , diff' x (mul' a b) := add' (mul' a (diff' x b)) (mul' b (diff' x a))  -- d-mul

  , diff' x (sin' x) := cos' x                               -- d-sin
  , diff' x (cos' x) := mul' (k (-1)) (sin' x)               -- d-cos

  , integral' (k 1) x := x                                   -- i-one
  , integral' (cos' x) x := sin' x                           -- i-cos
  , integral' (sin' x) x := mul' (k (-1)) (cos' x)           -- i-sin
  , integral' (add' f g) x := add' (integral' f x) (integral' g x)  -- i-sum
  , integral' (sub' f g) x := sub' (integral' f x) (integral' g x)  -- i-dif
  , integral' (mul' a b) x :=                                -- i-parts
      sub' (mul' a (integral' b x)) (integral' (mul' (diff' x a) (integral' b x)) x)
  ]

-- * The seven start terms

type T = Fix Math

var :: String -> T
var = Fix . Var

cst :: Int64 -> T
cst = Fix . Const

tAdd, tSub, tMul, tDiv, tPow, tDiff, tIntegral :: T -> T -> T
tAdd p q = Fix (Add p q)
tSub p q = Fix (Sub p q)
tMul p q = Fix (Mul p q)
tDiv p q = Fix (Div p q)
tPow p q = Fix (Pow p q)
tDiff p q = Fix (Diff p q)
tIntegral p q = Fix (Integral p q)

tLn, tSqrt, tCos :: T -> T
tLn p = Fix (Ln p)
tSqrt p = Fix (Sqrt p)
tCos p = Fix (Cos p)

start :: [T]
start =
  [ tIntegral (tLn vx) vx
  , tIntegral (tAdd vx (tCos vx)) vx
  , tIntegral (tMul (tCos vx) vx) vx
  , tDiff vx (tAdd (cst 1) (tMul (cst 2) vx))
  , tDiff vx (tSub (tPow vx (cst 3)) (tMul (cst 7) (tPow vx (cst 2))))
  , tAdd (tMul vy (tAdd vx vy)) (tSub (tAdd vx (cst 2)) (tAdd vx vx))
  , tDiv (cst 1) (tSub (tDiv (tAdd (cst 1) (tSqrt five)) (cst 2))
                       (tDiv (tSub (cst 1) (tSqrt five)) (cst 2)))
  ]
  where
    vx = var "x"
    vy = var "y"
    five = var "five"

-- * One iteration: match all rules, apply all matches, rebuild

-- | The relational database hegg's 'ematch' runs on, built from the canonical e-nodes of
-- every e-class. hegg's own 'Data.Equality.Matching.eGraphToDatabase' builds it from the
-- hashcons without canonicalizing the e-class ids stored there (its "ROMES:TODO map
-- find"); 'rebuild' leaves those ids stale after cascading merges, so a join between two
-- atoms of a pattern can fail on a stale id and matches go missing (on this workload
-- first in iteration 5: 771 instead of 784 e-nodes). Same fold, same tries, canonical ids.
canonicalDatabase :: G -> Database Math
canonicalDatabase egr = IM.foldlWithKey' addClass (DB M.empty) (classes egr)
  where
    addClass db cid cls =
      let !cid' = find cid egr
       in S.foldl' (\d n -> addNode cid' (canonicalize n egr) d) db (eClassNodes cls)
    addNode cid enode (DB m) = DB (M.alter (Just . populate (cid : children enode)) (operator enode) m)
    populate [] Nothing = MkIntTrie IS.empty IM.empty
    populate (y : ys) Nothing = MkIntTrie (IS.singleton y) (IM.singleton y (populate ys Nothing))
    populate [] (Just t) = t
    populate (y : ys) (Just (MkIntTrie ks m)) = MkIntTrie (IS.insert y ks) (IM.alter (Just . populate ys) y m)

-- | Returns the number of matches applied and the rebuilt e-graph.
step :: [Rewrite () Math] -> G -> (Int, G)
step rws egr0 =
  let db = canonicalDatabase egr0
      -- hegg's e-matching, on the database of the e-graph at the start of the iteration
      matches = [ (rhs, m) | lhs := rhs <- rws, m <- ematch db lhs ]
      (!n, !egr1) = foldl' (\(!i, !e) (rhs, Match subst cls) -> (i + 1, applyMatch rhs subst cls e))
                           (0 :: Int, egr0) matches
   in (n, rebuild egr1)

-- | Apply one match of a rule with right-hand side @rhs@ (as hegg's driver does): a
-- variable right-hand side merges two existing e-classes, otherwise the instantiated
-- right-hand side is added and merged with the e-class the left-hand side matched in.
applyMatch :: P -> Subst -> ClassId -> G -> G
applyMatch (VariablePattern v) subst cls e = snd (merge (substLookup v subst) cls e)
applyMatch (NonVariablePattern p) subst cls e =
  let (cls', e') = reprPat subst p e in snd (merge cls cls' e')

reprPat :: Subst -> Math P -> G -> (ClassId, G)
reprPat subst p e0 =
  let (p', e1) = runState (traverse go p) e0 in add (Node p') e1
  where
    go :: P -> State G ClassId
    go (VariablePattern v) = pure (substLookup v subst)
    go (NonVariablePattern q) = state (reprPat subst q)

substLookup :: Int -> Subst -> ClassId
substLookup v = IM.findWithDefault (error "unbound pattern variable") v

-- | Walk every e-class, e-node, parent and hashcons entry, so that no deferred work (a
-- thunk such as a `find` inside a canonicalized child that a map comparison never had
-- to look at) is left to be done outside the timed region.
forceGraph :: G -> Int
forceGraph egr = fromClasses + fromMemo
  where
    fromClasses = IM.foldlWithKey' (\acc cid cls ->
                    acc + find cid egr
                        + S.foldl' (\s n -> s + children n) 0 (eClassNodes cls)
                        + foldl' (\s (p, n) -> s + p + children n) 0 (toListSL (eClassParents cls)))
                  0 (classes egr)
    fromMemo = M.foldlWithKey' (\acc n cid -> acc + cid + children n) 0 (unNodeMap (memo egr))
    children (Node n) = foldl' (+) 0 n

-- | Canonical, deduplicated e-nodes of the final e-graph, per constructor. hegg keeps
-- stale (non-canonical) e-nodes in the e-class node sets and in the hashcons, so both
-- are canonicalized and deduplicated first; the two counts must agree.
sizes :: G -> (M.Map String Int, Int)
sizes egr = (M.fromListWith (+) [ (ctor (unNode n), 1) | n <- S.toList fromClasses ], S.size fromMemo)
  where
    fromClasses = S.fromList [ canonicalize n egr | cls <- IM.elems (classes egr), n <- S.toList (eClassNodes cls) ]
    fromMemo = S.fromList [ canonicalize n egr | n <- M.keys (unNodeMap (memo egr)) ]

main :: IO ()
main = do
  args <- getArgs
  let iters = case args of
        [] -> 11
        ["--iters", n] -> read n
        _ -> error ("usage: math-microbenchmark [--iters N], got " ++ show args)

  -- the start terms, in one e-graph, with invariants restored (as egg's port does)
  let egr0 = rebuild (foldl' (\e t -> snd (represent t e)) emptyEGraph start)
  _ <- evaluate (forceGraph egr0)

  let loop :: Int -> G -> [Int] -> IO (G, [Int])
      loop 0 e ms = return (e, reverse ms)
      loop i e ms = do
        let (n, e') = step rules e
        e'' <- evaluate e'
        loop (i - 1) e'' (n : ms)
  t0 <- getMonotonicTime
  (egr, matchCounts) <- loop iters egr0 []
  t1 <- getMonotonicTime
  _ <- evaluate (forceGraph egr)
  t2 <- getMonotonicTime
  let runS = t2 - t0

  let (perCtor, memoNodes) = sizes egr
      nodes = sum (M.elems perCtor)
      nClasses = IM.size (classes egr)
  printf "run_s %.6f\n" runS
  putStrLn "("
  mapM_ (\(name, n) -> printf " (%s %d)\n" name n) (M.toList perCtor)
  putStrLn ")"
  hPrintf stderr "hegg: %d iterations, %d e-nodes (%d via hashcons), %d e-classes, %.3fs (iterations %.3fs + final force %.3fs)\n"
    iters nodes memoNodes nClasses runS (t1 - t0) (t2 - t1)
  hPutStrLn stderr ("hegg: matches applied per iteration " ++ show matchCounts)
