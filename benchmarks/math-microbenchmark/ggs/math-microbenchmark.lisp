;;; ggs port of benchmarks/math-microbenchmark/math-microbenchmark.egg.
;;;
;;; Same language, the same 23 rewrites in the same order, the same seven start terms and the
;;; same number of iterations. ggs terms are (fsym arg...) with atoms as 0-ary function
;;; symbols: egglog's (Const i) is the integer i and (Var "x") is the symbol X, so Const and Var
;;; are counted by the type of the function symbol. ggs's RUN-REWRITES does what egglog's
;;; (run N) does: per iteration, every rule is matched against the index built by the last
;;; EGRAPH-REBUILD (new e-nodes are not visible until the next rebuild) and its matches are
;;; applied (intern the right-hand side, merge), then the e-graph is rebuilt. No match limit,
;;; no backoff scheduling (INITIAL-MATCH-LIMIT nil), no analysis, no extraction.
;;;
;;; Built into a standalone executable by systems/ggs/build-port.lisp.
;;; Usage: math-microbenchmark [--iters N]
;;; Prints run_s and e-nodes per constructor (egglog names, (print-size) format) on stdout.
(defpackage :ggs-port (:use :cl :ggs/eqsat) (:export #:main))
(in-package :ggs-port)

(declaim (optimize (speed 3) (safety 0) (debug 0)))
#+sbcl (declaim (sb-ext:muffle-conditions sb-ext:compiler-note))

;; The 23 rewrites, in the order of math-microbenchmark.egg. Rule matching code is generated
;; and natively compiled at compile time by PRECOMPILE-RULE-SET.
(defrw* math
  ((Add ?a ?b) (Add ?b ?a))
  ((Mul ?a ?b) (Mul ?b ?a))
  ((Add ?a (Add ?b ?c)) (Add (Add ?a ?b) ?c))
  ((Mul ?a (Mul ?b ?c)) (Mul (Mul ?a ?b) ?c))

  ((Sub ?a ?b) (Add ?a (Mul -1 ?b)))

  ((Add ?a 0) ?a)
  ((Mul ?a 0) 0)
  ((Mul ?a 1) ?a)

  ((Sub ?a ?a) 0)

  ((Mul ?a (Add ?b ?c)) (Add (Mul ?a ?b) (Mul ?a ?c)))
  ((Add (Mul ?a ?b) (Mul ?a ?c)) (Mul ?a (Add ?b ?c)))

  ((Mul (Pow ?a ?b) (Pow ?a ?c)) (Pow ?a (Add ?b ?c)))
  ((Pow ?x 1) ?x)
  ((Pow ?x 2) (Mul ?x ?x))

  ((Diff ?x (Add ?a ?b)) (Add (Diff ?x ?a) (Diff ?x ?b)))
  ((Diff ?x (Mul ?a ?b)) (Add (Mul ?a (Diff ?x ?b)) (Mul ?b (Diff ?x ?a))))

  ((Diff ?x (Sin ?x)) (Cos ?x))
  ((Diff ?x (Cos ?x)) (Mul -1 (Sin ?x)))

  ((Integral 1 ?x) ?x)
  ((Integral (Cos ?x) ?x) (Sin ?x))
  ((Integral (Sin ?x) ?x) (Mul -1 (Cos ?x)))
  ((Integral (Add ?f ?g) ?x) (Add (Integral ?f ?x) (Integral ?g ?x)))
  ((Integral (Sub ?f ?g) ?x) (Sub (Integral ?f ?x) (Integral ?g ?x)))
  ((Integral (Mul ?a ?b) ?x)
   (Sub (Mul ?a (Integral ?b ?x))
        (Integral (Mul (Diff ?x ?a) (Integral ?b ?x)) ?x))))
(precompile-rule-set math)

;; The seven start terms (constants are integers, variables are symbols).
(defparameter *start*
  '((Integral (Ln x) x)
    (Integral (Add x (Cos x)) x)
    (Integral (Mul (Cos x) x) x)
    (Diff x (Add 1 (Mul 2 x)))
    (Diff x (Sub (Pow x 3) (Mul 7 (Pow x 2))))
    (Add (Mul y (Add x y)) (Sub (Add x 2) (Add x x)))
    (Div 1 (Sub (Div (Add 1 (Sqrt five)) 2) (Div (Sub 1 (Sqrt five)) 2)))))

;; ggs function symbol -> egglog constructor name.
(defparameter *constructors*
  '((Diff . "Diff") (Integral . "Integral") (Add . "Add") (Sub . "Sub") (Mul . "Mul")
    (Div . "Div") (Pow . "Pow") (Ln . "Ln") (Sqrt . "Sqrt") (Sin . "Sin") (Cos . "Cos")))

(defun constructor-name (fsym)
  (cond ((integerp fsym) "Const")
        ((assoc fsym *constructors*) (cdr (assoc fsym *constructors*)))
        ((symbolp fsym) "Var")
        (t (error "unexpected function symbol ~s" fsym))))

(defun sizes ()
  "E-nodes per constructor in the (rebuilt) e-graph: the per-fsym node index that
EGRAPH-REBUILD builds from the hash-cons holds exactly the canonical, deduplicated e-nodes."
  (let ((counts (make-hash-table :test 'equal))
        (total 0))
    (maphash (lambda (fsym info)
               (let ((n (length (ggs/eqsat::fsym-info-nodes info))))
                 (incf (gethash (constructor-name fsym) counts 0) n)
                 (incf total n)))
             (ggs/eqsat::egraph-fsym-table *egraph*))
    (unless (= total (egraph-n-enodes *egraph*))
      (error "index holds ~a e-nodes but the hash-cons ~a" total (egraph-n-enodes *egraph*)))
    (sort (alexandria:hash-table-alist counts) #'string< :key #'car)))

(defun main ()
  (let ((iters 11)
        (args (rest sb-ext:*posix-argv*)))
    (loop while args
          do (let ((a (pop args)))
               (cond ((string= a "--iters") (setf iters (parse-integer (pop args))))
                     (t (error "unknown argument ~a" a)))))
    (let ((*egraph* (make-egraph)))
      (dolist (term *start*) (make-term term))
      (egraph-rebuild)
      (let* ((t0 (get-internal-real-time))
             (reason (run-rewrites 'math :max-iter iters))
             (run-s (/ (- (get-internal-real-time) t0)
                       (float internal-time-units-per-second 1d0))))
        (format t "run_s ~f~%(~%" run-s)
        (dolist (entry (sizes))
          (format t " (~a ~d)~%" (car entry) (cdr entry)))
        (format t ")~%")
        (finish-output)
        (format *error-output* "ggs: ~a iterations, ~a e-nodes, ~a e-classes, stop reason ~a, ~,3fs~%"
                iters (egraph-n-enodes *egraph*) (egraph-n-eclasses *egraph*) reason run-s)))))
