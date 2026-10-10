;;; ggs port of benchmarks/matmul-assoc/matmul-assoc-<n>.egg (from foresight's examples/mm):
;;; a chain of n multiplications of (Mat 10 10), the two associativity rules, run to saturation.
;;; ggs terms are (fsym arg...) with atoms as 0-ary function symbols, so (Mat 10 10) is the
;;; e-node MAT over the e-class of the integer 10; the integer is a leaf e-node of ggs (one
;;; e-class) that egglog has no table for, and it is not counted (like egg's i64 data nodes).
;;;
;;; Saturation as in benchmarks/horner/ggs/horner.lisp: ggs's own driver RUN-REWRITES, called
;;; with :max-iter 1 per iteration so that the port can count iterations, until it returns
;;; :SATURATE (e-node and e-class counts unchanged by an iteration; that iteration counts, as
;;; in egglog). No match limit, no :prune-constant, no analysis, no extraction.
;;;
;;; Built into a standalone executable by systems/ggs/build-port.lisp.
;;; Usage: matmul-assoc [--instance N]
;;; Prints iterations, run_s and e-nodes per constructor (egglog names, (print-size) format).
(defpackage :ggs-port (:use :cl :ggs/eqsat) (:export #:main))
(in-package :ggs-port)

(declaim (optimize (speed 3) (safety 0) (debug 0)))
#+sbcl (declaim (sb-ext:muffle-conditions sb-ext:compiler-note))

;; The two rewrites, in the order of matmul-assoc-<n>.egg (gen.py).
(defrw* matmul
  ((Mul (Mul ?x ?y) ?z) (Mul ?x (Mul ?y ?z)))                 ; mul-associativity1
  ((Mul ?x (Mul ?y ?z)) (Mul (Mul ?x ?y) ?z)))                ; mul-associativity2
(precompile-rule-set matmul)

(defun chain (n)
  "Mul(...Mul(Mul(Mat, Mat), Mat)..., Mat) with n Muls, Mat = (Mat 10 10)."
  (let ((e '(Mat 10 10)))
    (dotimes (i n e)
      (setf e `(Mul ,e (Mat 10 10))))))

;; ggs function symbol -> egglog constructor name; integer leaves are not counted.
(defparameter *constructors* '((Mat . "Mat") (Mul . "Mul")))

(defun constructor-name (fsym)
  (cond ((assoc fsym *constructors*) (cdr (assoc fsym *constructors*)))
        ((integerp fsym) nil)
        (t (error "unexpected function symbol ~s" fsym))))

(defun sizes ()
  "E-nodes per constructor in the (rebuilt) e-graph: the per-fsym node index that
EGRAPH-REBUILD builds from the hash-cons holds exactly the canonical, deduplicated e-nodes."
  (let ((counts (make-hash-table :test 'equal))
        (total 0))
    (maphash (lambda (fsym info)
               (let ((n (length (ggs/eqsat::fsym-info-nodes info)))
                     (name (constructor-name fsym)))
                 (when name (incf (gethash name counts 0) n))
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
  (let ((instance 20)
        (args (rest sb-ext:*posix-argv*)))
    (loop while args
          do (let ((a (pop args)))
               (cond ((string= a "--instance") (setf instance (parse-integer (pop args))))
                     (t (error "unknown argument ~a" a)))))
    (let ((*egraph* (make-egraph)))
      (make-term (chain instance))
      (egraph-rebuild)
      (let* ((t0 (get-internal-real-time))
             (iterations (saturate 'matmul))
             (run-s (/ (- (get-internal-real-time) t0)
                       (float internal-time-units-per-second 1d0))))
        (format t "iterations ~d~%run_s ~f~%(~%" iterations run-s)
        (dolist (entry (sizes))
          (format t " (~a ~d)~%" (car entry) (cdr entry)))
        (format t ")~%")
        (finish-output)
        ;; e-classes includes the one of the integer literal 10
        (format *error-output* "ggs: matmul-assoc-~a: ~a iterations, ~a e-nodes, ~a e-classes (incl. the literal 10), ~,3fs~%"
                instance iterations (egraph-n-enodes *egraph*) (egraph-n-eclasses *egraph*) run-s)))))
