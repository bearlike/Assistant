# mewbo-iam

Identity kernel for Mewbo — principals, authenticators, permissions/roles, teams, ownership grants, group→role/team mappings, user records, and an auth audit trail. Pure data-owned models plus JSON/Mongo stores; depends only on mewbo-core. Consumed by apps, never imports one.

Part of the [Mewbo](https://github.com/bearlike/Assistant) monorepo. See the
repository README for the architecture this package sits in, and
`packages/mewbo_iam/CLAUDE.md` for the doctrine that governs edits to it.
