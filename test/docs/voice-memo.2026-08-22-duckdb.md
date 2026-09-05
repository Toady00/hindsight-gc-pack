---
schema_version: 2
id: voice-memo.2026-08-22-duckdb
type: voice-memo
status: draft
title: Founder memo — DuckDB for analytics?
source: human
scope: platform
created_at: 2026-08-22T08:15:00Z
updated_at: 2026-08-22T08:15:00Z
---
Okay so, quick thought while it's fresh. I keep hitting this thing where the
analytics queries over report metrics are just... they're painful in
Postgres, we're doing these wide aggregations over what's basically columnar
data. I was reading about DuckDB last night and honestly for the analytics
side it might be a way better fit. Like, keep Postgres for the services
obviously, that's settled, I'm not reopening that. But maybe the analytics
pipeline reads the report JSON straight out of S3 with DuckDB instead of us
loading everything into Postgres first. I don't know. It might be a terrible
idea, I haven't costed it, haven't looked at how it plays with the Terraform
setup at all. Just... something to look into before we build the metrics
rollup thing. Anyway. End of thought.
