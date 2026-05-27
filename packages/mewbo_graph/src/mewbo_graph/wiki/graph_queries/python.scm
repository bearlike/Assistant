;; python.scm — tree-sitter capture patterns for the wiki graph

; Top-level classes
(module
  (class_definition
    name: (identifier) @class.name) @class.def)

; Top-level functions
(module
  (function_definition
    name: (identifier) @function.name) @function.def)

; Methods (functions inside class bodies)
(class_definition
  body: (block
          (function_definition
            name: (identifier) @method.name) @method.def))

; Class heritage (EXTENDS edge)
(class_definition
  name: (identifier) @subclass.name
  superclasses: (argument_list
                  (identifier) @superclass.name))

; Plain `import x` and `import x.y`
(import_statement
  name: (dotted_name) @import.module)

; `from x import y, z`
(import_from_statement
  module_name: (dotted_name) @import.from_module
  name: (dotted_name) @import.from_name)

; Direct calls — `foo()` or `obj.bar()`
(call
  function: [
    (identifier) @call.name
    (attribute attribute: (identifier) @call.name)
  ])
