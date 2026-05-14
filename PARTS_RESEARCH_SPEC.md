# Parts Research System Specification

## Purpose

This system researches spare parts by article number, stores all intermediate research data in a dedicated research database, and publishes only clean draft/final catalog records into the Smart database.

The system is built around a simple separation:

- `parts_research` is the working database. It stores tasks, runs, raw JSON artifacts, parsed draft data, Exa cache, source evidence, agent actions, and plugin-provided context.
- `smart_test` is the Smart catalog database. It stores only catalog-shaped results: parts, component relations, product types, and draft/final flags.

The agent workflow must be evidence-first. If information is not found, the field remains `null` or empty where the target schema allows it. The system must not invent facts to make the result look complete.

The implementation should avoid unnecessary abstractions and fallback logic. Required functionality should be explicit, testable, and easy to inspect through SQL.

## Core Workflow

The normal flow is:

1. A user submits one or more article numbers as research tasks.
2. The backend creates a separate research task and research run for each article.
3. A `research-agent` runs for each article in its own Codex thread.
4. The `research-agent` uses Exa to search the internet, using the backend Exa wrapper so exact repeated Exa requests are cached.
5. The research output is saved physically as raw JSON.
6. The backend parses the saved JSON deterministically into draft tables in `parts_research`.
7. A `curator/write-agent` can inspect the parsed draft data, source evidence, Smart context through FDW, and external plugin context.
8. The `curator/write-agent` decides how to write clean draft catalog records into Smart.
9. The system writes Smart records as drafts and unverified component relations by default.
10. A human later manually verifies and finalizes records directly in the database by changing draft/unverified flags.

The raw JSON and parsed draft data are both important. Raw JSON preserves the full artifact from a run. Parsed draft tables make the data searchable and usable for SQL, views, UI, and curator-agent decisions.

## Databases

### `parts_research`

`parts_research` is the main operational database for the research system.

It stores:

- research tasks and runs;
- raw JSON artifacts from Exa and Codex;
- parsed draft part data;
- parsed draft component data;
- source URLs and evidence text;
- exact Exa request cache;
- plugin-provided context items;
- SQL actions executed by agents;
- UI-facing queue and result state.

This database is allowed to contain incomplete, uncertain, draft, and conflicting information. It is the working memory of the system.

### `smart_test`

`smart_test` is the clean catalog target.

It stores:

- `parts`;
- `part_components`;
- `product_types`;
- `parts_with_components` view.

Smart records may still be drafts, but they should be catalog-shaped. Smart should not store raw Exa responses, long source evidence, old research artifacts, or agent logs.

### FDW Boundary

`parts_research` accesses `smart_test` through PostgreSQL `postgres_fdw`.

The intended setup is:

- install `postgres_fdw` in `parts_research`;
- create a foreign server pointing to `smart_test`;
- create a user mapping for the local research DB user;
- import or define Smart tables in a dedicated schema, preferably `smart`;
- let agents and backend services query Smart data from `parts_research` with normal SQL.

Example query shape:

```sql
select *
from smart.parts
where '807252T5' = any(articles);
```

Smart through FDW is also treated as a context source for agents. It can provide confirmed or draft catalog knowledge about existing parts, components, and kit relationships.

## Smart Data Model Assumptions

The Smart schema contains:

- `parts.id`: generated Smart ID;
- `parts.name`: catalog name;
- `parts.articles`: array of article numbers;
- `parts.brands`: array of allowed brands;
- `parts.description`: optional description;
- `parts.product_type`: product type;
- `parts.model`: model/application text;
- `parts.weight_kg`: weight in kilograms;
- `parts.is_draft`: draft flag;
- `part_components.parent_id`: kit or parent part;
- `part_components.child_id`: component part;
- `part_components.quantity`: component quantity;
- `part_components.can_be_sold_separately`;
- `part_components.is_unverified`;
- `parts_with_components`: view with direct components and computed `is_kit`.

`description` exists in Smart and should be used for normal parts when the research result contains a useful description. For draft components without article numbers, the system should only write `name` and not force a description.

## Agent Roles

### Research Agent

The `research-agent` is responsible for the initial deep research of a specific article number.

It:

- runs in its own Codex thread for each article;
- uses Exa heavily through the backend Exa wrapper;
- can make additional Exa requests after the required initial searches;
- produces a structured JSON result;
- records source-backed evidence for claims;
- keeps uncertainty explicit instead of overclaiming.

The `research-agent` may receive plugin-provided context. This context is useful but not authoritative. It can include Smart context, Avito-like marketplace data, custom database rows, or other future source plugins.

The `research-agent` should not receive old research results by default. Old research results may contain stale or bad intermediate conclusions. Exact Exa cache is still used automatically, because it represents the same Exa request returning the same previously stored response.

### Curator / Write Agent

The `curator/write-agent` is a judge and editor for writing data into Smart.

It:

- reads parsed draft data from `parts_research`;
- reads source/evidence data when needed;
- reads Smart context through FDW;
- can execute SQL through its SQL tool;
- can call Exa MCP directly when it needs to resolve a conflict or verify a fact;
- decides how to write clean draft records into Smart;
- does not delegate exact follow-up research to another agent.

The curator-agent should not be overloaded with all evidence up front. Its prompt should tell it to inspect the necessary draft, source, Smart, and plugin context before writing. It can choose what to read through SQL.

## Exa Cache

Exa caching is exact and intentionally simple.

If the same Exa tool is called with the exact same arguments, the backend returns the cached response. If any argument differs, the backend performs a new Exa request and stores the response.

This prevents paying repeatedly for identical Exa searches while avoiding fuzzy cache behavior.

The cache should store:

- request hash;
- tool name;
- full arguments JSON;
- full response JSON;
- creation time;
- last used time;
- hit count.

The request hash is based on the tool name and a stable serialization of the full arguments JSON. For example, changing `query`, `numResults`, or any other parameter creates a different cache key.

Exa cache and evidence are separate:

- Exa cache stores technical API responses.
- Evidence stores the specific source snippets and reasoning that the agent actually used for a draft or Smart write decision.

## Raw JSON And Draft Parsing

The system physically stores JSON artifacts from research runs.

Important raw artifacts include:

- initial Exa search responses;
- additional Exa search responses;
- low-confidence verification search responses;
- kit contents search responses;
- Codex structured result JSON;
- future direct-source plugin outputs.

After a JSON result is saved, the backend parses it into draft data in `parts_research`.

The parsing should be deterministic. The agent may create or update JSON, but the backend should be responsible for mapping JSON into draft database rows. This keeps database writes predictable.

Draft data should stay connected to:

- the original task;
- the specific run;
- the raw JSON result;
- the source/evidence rows used to support it.

## Draft Data In `parts_research`

The exact SQL schema can evolve, but the research database should minimally represent these concepts:

- tasks: submitted article numbers and current task status;
- runs: individual attempts, Codex thread IDs, start/end timestamps, and error state;
- raw artifacts: saved JSON payloads and their type;
- draft parts: parsed part-shaped data from a result;
- draft components: parsed kit/component data, including components without article numbers;
- evidence: source URLs and explanation text connected to draft fields or relations;
- external context items: plugin-provided hints and data;
- Exa cache: exact Exa request cache;
- agent SQL runs: SQL executed by agents and its result/error.

The purpose is not to build a complicated workflow engine. The purpose is to make research data queryable, inspectable, and publishable into Smart.

## Smart Publication Rules

Publishing to Smart means converting parsed draft research data into Smart-shaped catalog rows.

General rules:

- Smart writes are draft by default.
- Smart parts are written with `is_draft = true`.
- Smart component relations are written with `part_components.is_unverified = true`.
- Human verification happens later in the database.
- Auto-finalization is not part of the current design.

The publisher should map:

- `name` to `parts.name`;
- article numbers to `parts.articles`;
- OEM brand to `parts.brands`;
- Russian description to `parts.description` for normal parts;
- application/model text to `parts.model`;
- product type to `parts.product_type`;
- weight to `parts.weight_kg`;
- kit membership to `part_components`.

If a value is unknown and the Smart column allows `null`, write `null`.

## Kits And Components

A part is considered a kit if it has components.

In Smart, `is_kit` is not stored as a direct column. It is computed by the `parts_with_components` view based on whether the part has rows in `part_components` as `parent_id`.

The system should support:

- parts that are kits;
- parts that are components of other kits;
- kits with fully identified components;
- kits with partially identified components;
- draft components without known article numbers.

### Components With Article Numbers

If a component has a reliable article number, it can be published as a Smart draft part.

The system then creates a `part_components` relation from the kit to that component.

The relation should remain unverified by default.

### Components Without Article Numbers

Draft components without article numbers are allowed.

This exists because real sources sometimes say a kit includes an O-ring, seal, gasket, washer, or similar component without giving a component article number. Losing that information would make the kit composition incomplete.

If Smart is configured to allow draft parts with empty `articles`, then the system may publish a component without article numbers as:

- `parts.name` from the source;
- `parts.articles = '{}'`;
- `parts.is_draft = true`;
- `part_components.is_unverified = true`;
- no forced `description` for that component.

This is acceptable only for draft records. A human should later add a real article number or decide how to handle the component.

This design intentionally accepts that Smart can contain incomplete draft data. The draft flags make the incompleteness explicit and prevent treating the data as final.

## Product Types And Brands

The system should use configured Smart product types and brands.

Expected product types include:

- `Для автомобилей`;
- `Для мототехники`;
- `Для водного транспорта`.

Expected brands come from Smart configuration. For Mercury, MerCruiser, Quicksilver, and Mariner, the research rules treat the OEM brand as Mercury Marine and map it to the appropriate Smart brand value.

The exact brand mapping should be centralized in backend config rather than redefined in every prompt.

## Additional Source Plugins

The system should support additional context sources through plugins.

Examples:

- Smart context through FDW;
- Avito or other marketplace listing data;
- custom databases;
- old internal datasets;
- future direct source connectors.

Plugin context is not guaranteed truth. It is a hint that the agent can consider. The agent may use it to decide what to verify through Exa or SQL, but it should not blindly publish plugin data as fact.

Plugin-provided data should be stored in `parts_research` as context connected to the relevant task or run.

The prompt should explain to agents that plugin data is preliminary and should be treated carefully.

## Old Research Results

Old research results should not be sent to the `research-agent` by default.

They may contain intermediate errors, old assumptions, or low-confidence conclusions. Passing them up front can bias a new research run.

However, old research data remains stored in `parts_research`. The curator-agent may inspect it through SQL if it decides that doing so is useful.

Smart data is different. Smart data is the catalog output and can be used as a context source through FDW.

## SQL Tool

The curator-agent should have a SQL tool that can execute SQL against `parts_research`.

Through FDW, that same SQL session can also inspect and write Smart tables.

The SQL tool is not technically restricted by query type. The agent can run SELECT, INSERT, UPDATE, DELETE, or other SQL when needed.

The agent instructions should still tell the agent to be careful, inspect before modifying, and keep destructive actions deliberate. SQL executions should be logged in `parts_research` so they can be audited later.

Database backups are assumed to exist, so the design does not require mandatory UI approval for every destructive SQL action.

## UI Expectations

The frontend will be built with Next.js.

The chat and agent streaming should use Vercel AI SDK v6 patterns such as `useChat`, `streamText`, tool calls, and streaming UI/data parts.

The UI should show:

- task submission sidebar;
- queue counts: submitted, running, completed, failed;
- task progress cards by article;
- modal or detail panel for each task result;
- errors visible in task detail;
- raw/draft/Smart publication status;
- Exa cache hit/miss when useful;
- visible agent actions that can later collapse;
- chat with curator/write-agent;
- source/evidence inspection;
- draft completeness and missing article indicators.

The visual design should be polished and clear. It should not be a generic dashboard. The interface should help a human quickly understand which parts are researched, which are weak, which are draft-only, and which records have been written to Smart.

## Implementation Order

The backend should be implemented before the frontend.

Recommended order:

1. Configure FDW from `parts_research` to `smart_test`.
2. Create minimal `parts_research` tables.
3. Implement exact Exa cache wrapper.
4. Implement raw JSON storage.
5. Implement deterministic JSON-to-draft parser.
6. Import current saved JSON examples into draft tables.
7. Implement draft-to-Smart publisher.
8. Implement SQL tool logging.
9. Connect Codex SDK threads for research runs.
10. Connect curator/write-agent flow.
11. Test with existing Mercury examples.
12. Build the Next.js UI after the backend flow works.

The first meaningful backend test is:

- ingest `codex_results/76868A04.json`;
- parse it into `parts_research`;
- publish a Smart draft part and any publishable component relations;
- repeat for `codex_results/807252T5.json`;
- verify Smart rows through FDW.

## Fixed Decisions

- `parts_research` is the working research database.
- `smart_test` is the clean catalog target.
- `parts_research` accesses Smart through PostgreSQL FDW.
- Full JSON artifacts are physically saved.
- Saved JSON is parsed into draft tables automatically.
- Draft data is connected to tasks and runs.
- Exa cache is exact-match only.
- Exa cache is based on identical tool arguments.
- Old research results are not sent to the research-agent by default.
- Smart context through FDW can be provided as plugin/context.
- Other source plugins may provide hints, not guaranteed truth.
- Curator/write-agent can call Exa directly.
- Curator/write-agent can execute SQL directly.
- Curator/write-agent does not delegate exact follow-up research to other agents.
- Research-agent can make additional Exa searches after mandatory searches.
- Evidence is stored in `parts_research`, not in Smart.
- Smart stores clean catalog-shaped records.
- Smart writes are drafts by default.
- Human finalization happens manually in the database.
- Components without article numbers may be represented as Smart drafts if Smart permits empty `articles` for draft rows.
- Draft components without article numbers should use `name`; description is not forced for them.
- No separate Smart `is_kit` column is needed.
- Views are useful and should be added where they simplify agent or UI work.

## Explicit Non-Goals

The current design does not include:

- fuzzy Exa cache matching;
- sending old research results to every new research-agent run by default;
- a complex workflow engine;
- automatic finalization of Smart records;
- mandatory approval for every SQL write;
- storing raw source evidence inside Smart tables;
- treating marketplace/plugin data as authoritative truth;
- forcing complete kit composition before publishing a draft kit.

## Summary

The system should keep research messy where mess is useful and keep Smart clean where catalog quality matters.

`parts_research` stores all research context, raw artifacts, draft parsing, evidence, Exa cache, and agent activity. `smart_test` stores only catalog-shaped output through normal Smart tables and draft flags.

The agents should have enough tools to work independently, but the backend should keep deterministic responsibilities such as caching, raw artifact storage, JSON parsing, and Smart publication mapping clear and testable.
