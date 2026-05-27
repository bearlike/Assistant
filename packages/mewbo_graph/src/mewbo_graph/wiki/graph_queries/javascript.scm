;; javascript.scm — tree-sitter capture patterns for the wiki graph

; Top-level classes
(class_declaration
  name: (identifier) @class.name) @class.def

; Top-level functions
(function_declaration
  name: (identifier) @function.name) @function.def

; Methods inside class body
(class_declaration
  body: (class_body
          (method_definition
            name: (property_identifier) @method.name) @method.def))

; Class heritage (EXTENDS edge) — `class Sub extends Base`
(class_declaration
  name: (identifier) @subclass.name
  (class_heritage
    (identifier) @superclass.name))

; ES6 imports — `import x from 'mod'` / `import { y } from 'mod'`
(import_statement
  source: (string) @import.module)

; Calls — `foo()` or `obj.bar()`
(call_expression
  function: [
    (identifier) @call.name
    (member_expression
      property: (property_identifier) @call.name)
  ])
