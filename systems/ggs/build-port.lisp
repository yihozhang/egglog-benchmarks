;;; Image-side helper for systems/ggs: load ggs/eqsat through Quicklisp and turn one port
;;; (benchmarks/<suite>/ggs/<name>.lisp, which defines package GGS-PORT with a function MAIN)
;;; into a standalone SBCL executable, so that a run pays neither ASDF loading nor compilation.
;;; Used from the Containerfile:  sbcl --non-interactive --load build-port.lisp --eval '(ggs-build:...)'
(defpackage :ggs-build (:use :cl) (:export #:prepare #:build))
(in-package :ggs-build)

(defparameter *dependencies*
  '(:alexandria :serapeum :metabang-bind :trivial-garbage :trivial-package-local-nicknames
    :linear-programming)
  "ggs/eqsat's Quicklisp dependencies; compiled under SBCL's default policy.")

(defparameter *policy* '(optimize (speed 3) (safety 0) (debug 0))
  "Global policy under which the engine itself (lp-hash-table, ggs/common, ggs/eqsat) is
compiled; ggs declares this locally in its hot paths already, this extends it to the rest
(about 7% on math-microbenchmark). Ports declare their own policy.")

(defun prepare ()
  "Load ggs/eqsat (compiling it and its dependencies into the fasl cache)."
  (load "/opt/quicklisp/setup.lisp")
  (setf (symbol-value (find-symbol "*LOCAL-PROJECT-DIRECTORIES*" :ql)) (list #p"/opt/lisp/"))
  (funcall (find-symbol "QUICKLOAD" :ql) *dependencies* :silent t)
  (proclaim *policy*)
  (proclaim '(sb-ext:muffle-conditions sb-ext:compiler-note))
  (funcall (find-symbol "QUICKLOAD" :ql) :ggs/eqsat :silent t))

(defun build (port out)
  "Compile PORT natively, load it, and save an executable OUT whose toplevel is GGS-PORT::MAIN.
The executable keeps this process's runtime options (--dynamic-space-size), so its own
command line goes to the port untouched."
  (prepare)
  (let ((fasl (compile-file port :output-file (make-pathname :type "fasl" :defaults port))))
    (unless fasl (error "compiling ~a failed" port))
    (load fasl))
  (let ((main (find-symbol "MAIN" :ggs-port)))
    (unless main (error "~a does not define GGS-PORT::MAIN" port))
    (sb-ext:disable-debugger)
    (sb-ext:save-lisp-and-die out :executable t :save-runtime-options t
                                  :toplevel (lambda ()
                                              (handler-case (funcall main)
                                                (error (e)
                                                  (format *error-output* "~&ggs port error: ~a~%" e)
                                                  (sb-ext:exit :code 1 :abort t)))
                                              (sb-ext:exit :code 0)))))
