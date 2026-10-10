;;; math-microbenchmark with (run 12): loads the base port and changes its default
;;; iteration count. Built like any port (compile-file + load + save-lisp-and-die).
;;; The base must be loaded at compile time too, so that its package exists when the
;;; rest of this file is read.
(eval-when (:compile-toplevel :load-toplevel :execute)
  (load (compile-file (merge-pathnames "math-microbenchmark.lisp"
                                       (or *compile-file-truename* *load-truename*)))))
(setf (symbol-value (intern "*DEFAULT-ITERS*" "GGS-PORT")) 12)
