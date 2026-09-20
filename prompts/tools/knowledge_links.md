List the notes linked to a note — the vault's graph, one hop.

Arguments:
- `path` (required): the vault-relative path of the note to start from.
- `direction` (required): `out` for the notes this one links to, `in` for the notes that
  link to it (backlinks).

Returns paths with their titles and types. Use it to find the procedure attached to a
device, the project a person appears in, or what would be affected by changing a note.
Unresolved links (targets with no note yet) are not listed; write the note to create it.
