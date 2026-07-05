;; java.scm — tree-sitter capture patterns for the wiki graph
;;
;; Unlike Kotlin, Java's grammar gives every declaration this file captures a
;; `name:` field, and interface/enum/record are each their OWN node type
;; (never `class_declaration`) — so, unlike kotlin.scm, there is no
;; generic-vs-specific pattern overlap to dedupe here.

; Interfaces
(interface_declaration
  name: (identifier) @interface.name) @interface.def

; Enum → Class + subkind "enum"
(enum_declaration
  name: (identifier) @class.name) @class.def @class.subkind.enum

; Record → Class + subkind "record"
(record_declaration
  name: (identifier) @class.name) @class.def @class.subkind.record

; Plain class
(class_declaration
  name: (identifier) @class.name) @class.def

; Class heritage — extends only (Java allows one superclass; `implements` is
; deliberately NOT captured here, same precedent as typescript.scm's
; extends-only choice). subclass.name/superclass.name is a GLOBAL per-file 1:1
; zip in graph.py — a class also emitting implements captures would push the
; ratio to 1 subclass : 2 superclasses for every ordinary "extends X
; implements Y" class (Java's single most common heritage shape, unlike
; Python's genuinely rare multi-base case that motivated the "pre-existing
; limitation" comment there), which would misalign EVERY subsequent
; heritage-bearing class in the same file — a needless amplification of an
; already-documented, out-of-scope zip limitation.
; A bare `extends Base` superclass field is a plain `type_identifier`; a
; package-qualified one (`extends pkg.sub.Base`, no import) is instead a
; `scoped_type_identifier` — recursively nested one level per `.` segment,
; so anchoring the LAST direct-child `type_identifier` (the trailing `.`)
; grabs "Base" regardless of qualifier depth without recursing by hand.
(class_declaration
  name: (identifier) @subclass.name
  superclass: (superclass
                [
                  (type_identifier) @superclass.name
                  (scoped_type_identifier
                    (type_identifier) @superclass.name .)
                ]))

; Methods (Java has no top-level functions; constructors are a distinct
; `constructor_declaration` node, deliberately not modeled here)
(method_declaration
  name: (identifier) @method.name) @method.def

; Fields
(field_declaration
  declarator: (variable_declarator
                name: (identifier) @property.name)) @property.def

; Imports — full dotted path via the (possibly nested) scoped_identifier;
; only the OUTER node is a direct child of import_declaration, so the query
; can't accidentally capture an inner segment.
(import_declaration
  (scoped_identifier) @import.module)

; Calls
(method_invocation
  name: (identifier) @call.name)
