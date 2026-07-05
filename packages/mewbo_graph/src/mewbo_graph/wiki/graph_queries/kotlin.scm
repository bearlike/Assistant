;; kotlin.scm — tree-sitter capture patterns for the wiki graph
;;
;; Kotlin's grammar positions every name captured here WITHOUT a `name:`
;; field (class/object/function names are plain positional children) — same
;; shape as python.scm, so `_by_position` (or the containment-based
;; `_pair_defs_with_names`) re-aligns def↔name pairs.
;;
;; `class_declaration` is Kotlin's ONE grammar node for interface / plain
;; class / data class / sealed class / enum class — the discriminator is an
;; anonymous keyword child ("interface", "enum") or a `modifiers` subtree
;; (data/sealed). The generic "class.def/name" pattern below matches every
;; class_declaration carrying a literal "class" keyword (every variant EXCEPT
;; interface); data/sealed/enum classes ALSO match one of the subkind
;; patterns beneath it, which independently re-capture the SAME class.def/
;; name pair — `_extract`'s node-id fold keeps the subkind-bearing duplicate.

; Interfaces
(class_declaration
  "interface"
  (type_identifier) @interface.name) @interface.def

; Enum class
(class_declaration
  "enum" @class.subkind.enum_class
  "class"
  (type_identifier) @class.name) @class.def

; Data class
(class_declaration
  (modifiers (class_modifier "data" @class.subkind.data_class))
  "class"
  (type_identifier) @class.name) @class.def

; Sealed class
(class_declaration
  (modifiers (class_modifier "sealed" @class.subkind.sealed_class))
  "class"
  (type_identifier) @class.name) @class.def

; Plain class (also matches data/sealed/enum above — dedup fold keeps the
; subkind-bearing variant)
(class_declaration
  "class"
  (type_identifier) @class.name) @class.def

; Class heritage (EXTENDS edge) — first supertype only, python.scm precedent.
; The `.` anchor after `:` matches only the FIRST delegation_specifier (a
; later one is separated from `:` by a comma-preceded sibling, breaking
; adjacency). Deliberately scoped to `class_declaration` only — NOT
; `object_declaration` — because the existing EXTENDS edge-emission code
; always synthesizes its subclass id with kind="Class" (see graph.py); an
; Object subclassing something would silently mint a dangling source id,
; replicating the same out-of-scope bug the extension REFERENCES edge below
; carefully avoids.
;
; The trailing `.` inside `user_type` anchors the LAST type_identifier — a
; qualified supertype (`pkg.sub.Base()`) is a FLAT `user_type` with one
; type_identifier per segment, so an unanchored capture here would grab
; "pkg"/"sub"/"Base" as three separate (wrong) superclass.name matches.
(class_declaration
  "class"
  (type_identifier) @subclass.name
  ":"
  .
  (delegation_specifier
    [
      (constructor_invocation (user_type (type_identifier) @superclass.name .))
      (user_type (type_identifier) @superclass.name .)
    ]))

; `object Foo { ... }` / `object Foo : Base() { ... }` — always named. Scoped
; to source_file/class_body (like function.def/property.def) so a LOCAL
; object declaration inside a function body isn't minted as a top-level node.
(source_file
  (object_declaration
    (type_identifier) @object.name) @object.def)

(class_body
  (object_declaration
    (type_identifier) @object.name) @object.def)

; `companion object` / `companion object Name` — the name is OPTIONAL in the
; grammar (an anonymous companion has no `type_identifier` token); `_extract`
; defaults an unnamed one to the literal "Companion" (Kotlin's own implicit
; name for it).
(companion_object
  (type_identifier)? @object.name) @object.def @object.subkind.companion_object

; Top-level functions (module scope) — NOT inside a class/object/companion body.
(source_file
  (function_declaration
    (simple_identifier) @function.name) @function.def)

; Extension functions — `fun Receiver.name()`, TOP-LEVEL only (mirrors the
; function.def scope above; the REFERENCES-edge source id `_extract` computes
; for these hardcodes kind="Function", so a class-member extension would need
; a matching Method-kind recipe there too — out of scope for this pass).
(source_file
  (function_declaration
    receiver: (receiver_type) @extension.receiver
    (simple_identifier) @extension.name) @extension.def)

; Methods — any function_declaration inside a class/object/companion body,
; OR an enum class body (after the `;` member separator) — `enum class_body`
; is a DISTINCT node kind (`enum_class_body`) from a plain `class_body`.
(class_body
  (function_declaration
    (simple_identifier) @method.name) @method.def)

(enum_class_body
  (function_declaration
    (simple_identifier) @method.name) @method.def)

; Properties (fields / constants) — top-level or member (val/var). Scoped to
; source_file/class_body/enum_class_body directly, like function.def/
; method.def above, so a LOCAL val/var inside a function body — also a
; property_declaration in this grammar — is NOT captured as a graph node.
(source_file
  (property_declaration
    (variable_declaration
      (simple_identifier) @property.name)) @property.def)

(class_body
  (property_declaration
    (variable_declaration
      (simple_identifier) @property.name)) @property.def)

(enum_class_body
  (property_declaration
    (variable_declaration
      (simple_identifier) @property.name)) @property.def)

; Primary-constructor properties — `class Foo(val x: Int, var y: String)`, the
; single most idiomatic Kotlin property form. A `class_parameter` carries a
; `binding_pattern_kind` (val/var) child ONLY when it's promoted to a
; property; a plain constructor parameter (no val/var) has neither and is
; correctly left uncaptured.
(class_parameter
  (binding_pattern_kind)
  (simple_identifier) @property.name) @property.def

; Imports — `import a.b.C`, `import a.b.*`, `import a.b.C as D` (alias and
; wildcard suffixes fall outside the captured `identifier`, so the module
; path text is always the plain dotted path).
(import_header
  (identifier) @import.module)

; Calls — bare `foo()` or navigation `obj.foo()` / `this.foo()`.
(call_expression
  [
    (simple_identifier) @call.name
    (navigation_expression
      (navigation_suffix
        (simple_identifier) @call.name))
  ])
