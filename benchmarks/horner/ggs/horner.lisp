;;; ggs port of benchmarks/horner/horner-<n>.egg (Horner's method, from foresight's examples/poly).
;;;
;;; Same language (Peano exponents), the same 11 rewrites in the same order, the polynomial of
;;; degree n built like gen.py does, run to saturation. ggs terms are (fsym arg...) with atoms
;;; as 0-ary function symbols: egglog's (Zero) is the 0-ary symbol ZERO and (Var "a") is the
;;; symbol A, so Var is counted as "every symbol that is not a constructor".
;;;
;;; Saturation: ggs's own driver RUN-REWRITES stops (reason :SATURATE) when an iteration
;;; (match every rule against the index built by the last EGRAPH-REBUILD, apply all matches,
;;; rebuild) leaves the e-node and e-class counts unchanged; that final iteration counts, as
;;; in egglog. RUN-REWRITES only returns the stop reason, so to report the iteration count the
;;; port calls it with :max-iter 1 once per iteration until it returns :SATURATE: the loop
;;; body and the stop rule are the engine's, unchanged (the count snapshot it takes at entry
;;; is the one after the previous rebuild, as inside its own loop). No match limit, no backoff
;;; scheduling, no :prune-constant, no analysis, no extraction.
;;;
;;; Built into a standalone executable by systems/ggs/build-port.lisp.
;;; Usage: horner [--instance N]
;;; Prints iterations, run_s and e-nodes per constructor (egglog names, (print-size) format).
(defpackage :ggs-port (:use :cl :ggs/eqsat) (:export #:main))
(in-package :ggs-port)

(declaim (optimize (speed 3) (safety 0) (debug 0)))
#+sbcl (declaim (sb-ext:muffle-conditions sb-ext:compiler-note))

;; The 11 rewrites, in the order of horner-<n>.egg (gen.py).
(defrw* horner
  ((Add ?x ?y) (Add ?y ?x))                                   ; add-commutativity
  ((Mul ?x ?y) (Mul ?y ?x))                                   ; mul-commutativity
  ((Add (Add ?x ?y) ?z) (Add ?x (Add ?y ?z)))                 ; add-associativity1
  ((Add ?x (Add ?y ?z)) (Add (Add ?x ?y) ?z))                 ; add-associativity2
  ((Mul (Mul ?x ?y) ?z) (Mul ?x (Mul ?y ?z)))                 ; mul-associativity1
  ((Mul ?x (Mul ?y ?z)) (Mul (Mul ?x ?y) ?z))                 ; mul-associativity2
  ((Mul ?x (Add ?a ?b)) (Add (Mul ?x ?a) (Mul ?x ?b)))        ; distributivity1
  ((Add (Mul ?x ?a) (Mul ?x ?b)) (Mul ?x (Add ?a ?b)))        ; distributivity2
  ((Mul ?x (Succ (Zero))) ?x)                                 ; multiplicative-identity
  ((Pow ?x (Zero)) (Succ (Zero)))                             ; power-null
  ((Pow ?x (Succ ?n)) (Mul ?x (Pow ?x ?n))))                  ; power-reduction
(precompile-rule-set horner)

(defun constant (i)
  "Peano i: (Zero), (Succ (Zero)), ..."
  (if (zerop i) '(Zero) `(Succ ,(constant (1- i)))))

(defun polynomial (n)
  "((a_n x^n + a_{n-1} x^{n-1}) + ...) + a_0, coefficients a, b, c, ... = a_0, a_1, ..."
  (when (> n 22) (error "degree ~a: coefficient symbols would reach x" n))
  (flet ((coeff (i) (intern (string (char-upcase (code-char (+ (char-code #\a) i)))))))
    (let ((terms (loop for i from 0 to n
                       collect (if (zerop i) (coeff 0) `(Mul ,(coeff i) (Pow x ,(constant i)))))))
      (reduce (lambda (acc term) `(Add ,acc ,term)) (reverse terms)))))

;; ggs function symbol -> egglog constructor name.
(defparameter *constructors*
  '((Zero . "Zero") (Succ . "Succ") (Add . "Add") (Mul . "Mul") (Pow . "Pow")))

(defun constructor-name (fsym)
  (cond ((assoc fsym *constructors*) (cdr (assoc fsym *constructors*)))
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

(defun saturate (rule-set)
  "Run RULE-SET with ggs's driver until its saturation check fires; return the iteration count."
  (loop for iterations from 1
        for reason = (run-rewrites rule-set :max-iter 1)
        do (ecase reason
             (:saturate (return iterations))
             (:max-iter))))

(defun main ()
  (let ((instance 5)
        (args (rest sb-ext:*posix-argv*)))
    (loop while args
          do (let ((a (pop args)))
               (cond ((string= a "--instance") (setf instance (parse-integer (pop args))))
                     (t (error "unknown argument ~a" a)))))
    (let ((*egraph* (make-egraph)))
      (make-term (polynomial instance))
      (egraph-rebuild)
      (let* ((t0 (get-internal-real-time))
             (iterations (saturate 'horner))
             (run-s (/ (- (get-internal-real-time) t0)
                       (float internal-time-units-per-second 1d0))))
        (format t "iterations ~d~%run_s ~f~%(~%" iterations run-s)
        (dolist (entry (sizes))
          (format t " (~a ~d)~%" (car entry) (cdr entry)))
        (format t ")~%")
        (finish-output)
        (format *error-output* "ggs: horner-~a: ~a iterations, ~a e-nodes, ~a e-classes, ~,3fs~%"
                instance iterations (egraph-n-enodes *egraph*) (egraph-n-eclasses *egraph*) run-s)))))
