;; typescript.scm — tree-sitter capture patterns for the wiki graph
;; Extends JS patterns with interface support. TypeScript uses type_identifier
;; for class/interface names instead of identifier.

; Top-level classes — TypeScript uses type_identifier for class names
(class_declaration
  name: (type_identifier) @class.name) @class.def

; Top-level functions
(function_declaration
  name: (identifier) @function.name) @function.def

; Methods inside class body
(class_declaration
  body: (class_body
          (method_definition
            name: (property_identifier) @method.name) @method.def))

; Class heritage — `class Sub extends Base`
(class_declaration
  name: (type_identifier) @subclass.name
  (class_heritage
    (extends_clause
      value: (identifier) @superclass.name)))

; Interfaces — `interface Foo { ... }` (may be inside export_statement)
(interface_declaration
  name: (type_identifier) @interface.name) @interface.def

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
