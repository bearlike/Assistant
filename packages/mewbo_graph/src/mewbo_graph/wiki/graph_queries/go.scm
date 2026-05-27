;; go.scm — tree-sitter capture patterns for the wiki graph
;; Go has no class keyword; structs map to Class, interfaces to Interface.

; Top-level functions
(function_declaration
  name: (identifier) @function.name) @function.def

; Methods — `func (r *Recv) Name(...)`
(method_declaration
  name: (field_identifier) @method.name) @method.def

; Struct types → Class
(type_declaration
  (type_spec
    name: (type_identifier) @class.name
    type: (struct_type)) @class.def)

; Interface types → Interface
(type_declaration
  (type_spec
    name: (type_identifier) @interface.name
    type: (interface_type)) @interface.def)

; Imports — single `import "pkg"` or grouped `import_spec` entries
(import_spec
  path: (interpreted_string_literal) @import.module)

; Calls — `foo()` or `pkg.Func()`
(call_expression
  function: [
    (identifier) @call.name
    (selector_expression
      field: (field_identifier) @call.name)
  ])
