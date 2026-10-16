-- 0009: each note's vector records the model that produced it (2026-10-15). Vectors from
-- different embedding models are not comparable (a size mismatch scored 0, a same-size
-- mismatch scored noise), and nothing re-embedded when the embedding role changed. The
-- index now uses only vectors whose `embed_model` is the embedding role's current model
-- and re-embeds the rest. Existing vectors carry no model, so they are re-embedded once.

ALTER TABLE notes ADD COLUMN embed_model TEXT;
