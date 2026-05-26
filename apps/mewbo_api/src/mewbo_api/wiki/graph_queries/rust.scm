;; rust.scm — tree-sitter capture patterns for the wiki graph
;; Structs map to Class, traits to Interface.

; Struct items → Class
(struct_item
  name: (type_identifier) @class.name) @class.def

; Trait items → Interface
(trait_item
  name: (type_identifier) @interface.name) @interface.def

; Top-level functions (outside impl blocks)
(function_item
  name: (identifier) @function.name) @function.def

; Methods inside impl blocks
(impl_item
  body: (declaration_list
          (function_item
            name: (identifier) @method.name) @method.def))

; `use foo::bar::Baz;`
(use_declaration
  argument: (_) @import.module)

; Calls — `foo()`, `self.method()`, `Type::assoc()`
(call_expression
  function: [
    (identifier) @call.name
    (field_expression
      field: (field_identifier) @call.name)
    (scoped_identifier
      name: (identifier) @call.name)
  ])
