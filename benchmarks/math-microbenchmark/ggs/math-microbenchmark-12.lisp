;;; math-microbenchmark with (run 12): loads the base port and changes its default
;;; iteration count. Built like any port (compile-file + load + save-lisp-and-die).
(load (compile-file (merge-pathnames "math-microbenchmark.lisp"
                                     (or *compile-file-truename* *load-truename*))))
(setf ggs-port::*default-iters* 12)
