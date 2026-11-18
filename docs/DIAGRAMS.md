# Diagrams

## Application Architecture

```mermaid
graph TD
    subgraph app["app/"]
        main["main.py<br/>(app factory, auth hook, Services)"]
        bp["blueprint.py<br/>(/chain, /health-check)"]
        config["config.py<br/>(Settings, logging)"]
        docs["docs.py<br/>(Swagger from the models)"]
        models["models.py<br/>(ChainQuery, responses)"]
        cache["cache.py<br/>(Redis, fails open)"]
        subgraph dcma["iati_cloud_dcma/"]
            service["service.py<br/>(one request)"]
            traversal["traversal.py<br/>(walks, modes, limits)"]
            tree["tree.py<br/>(Chain → tree)"]
            source["source.py<br/>(LinkSource, Solr)"]
            links["links.py<br/>(document → links)"]
            nodes["nodes.py<br/>(node keys)"]
        end
    end

    main --> config
    main --> bp
    main --> docs
    bp --> service
    service --> cache
    service --> traversal
    service --> tree
    service --> models
    traversal --> source
    source --> links
    links --> nodes
    config -->|loads| env[".env"]
    source -->|select queries| solr[("IATI.cloud Solr<br/>activity core")]
    cache --> redis[("Redis")]
```

## Request Flow

```mermaid
sequenceDiagram
    participant C as Client (server side)
    participant A as before_request
    participant R as GET /chain
    participant K as Redis cache
    participant T as Traversal
    participant S as Solr

    C->>A: GET /chain?activity=…
    alt wrong or missing API key
        A-->>C: 401
    else
        A->>R: forward
        R->>R: validate query (400 if invalid)
        R->>K: get(key)
        alt cached
            K-->>R: payload
        else not cached, or Redis down
            R->>T: build_chain(seed, mode, limits, orgs)
            loop each hop
                T->>S: fetch identifiers, reverse lookup
                S-->>T: documents
            end
            T-->>R: Chain
            R->>K: set(key, payload), unless cut off by the time budget
        end
        R-->>C: 200 graph or tree
    end
```

Errors: `404` when the seed is neither published nor referenced, `502` on a Solr error, `504` on a Solr timeout.

## Link Model

X is the activity whose document is read. Solid arrows are links between activities; dashed arrows end in an
organisation, where no activity is named. Arrows point from funder to funded.

```mermaid
graph TD
    P["parent / provider activity / funder activity"] -->|"L1 related-activity type 1<br/>L3 provider-activity-id<br/>L5 participating-org role 1 + activity-id"| X(("X"))
    OP["organisation: provider or funder"] -.->|"O2 provider-org without activity ID<br/>O3 role 1 without activity ID"| X
    X -->|"L2 related-activity type 2<br/>L4 receiver-activity-id, outgoing transactions only<br/>L6 role 4 + activity-id, not the reporting org"| C["child / receiver activity / implementer activity"]
    X -.->|"O1 receiver-org without activity ID, outgoing transactions only"| OR["organisation: receiver"]
    X ---|"L7 role 2 or 3 + activity-id (association, not followed)"| A["associated activity"]
```

## Traversal

```mermaid
graph TD
    subgraph full["mode=full"]
        CF["co-funder<br/>(not expanded)"] -.-> R
        A1["ancestor"] --> S1(("seed"))
        A1 --> R["related"]
        S1 --> D1["descendant"]
    end
    subgraph constrained["mode=constrained"]
        A2["ancestor"] --> S2(("seed"))
        S2 --> D2["descendant"]
    end
```

Each hop of a walk fetches the frontier's documents by identifier (all copies of a duplicated identifier, merged),
then runs one reverse lookup for the documents that name them. A walk stops at `max_depth`, `max_nodes`, the reverse
lookup limit or the request's time budget, and the response lists which in `truncation_reasons`.

## Deployment

```mermaid
graph LR
    compose["docker-compose.yaml"]
    compose -->|builds| image["Docker image<br/>(python:3.11-slim + uv)"]
    compose -->|starts| redis["redis:7.4-alpine<br/>(cache only, no persistence)"]
    compose -->|sets| env2["SECRET_KEY, SOLR_URL, REDIS_URL, …"]
    compose -->|mounts| vols["app/, data/, logs/"]
    image -->|runs| gunicorn["gunicorn<br/>0.0.0.0:PORT, 4 workers, 60 s timeout"]
    gunicorn -->|serves| flask["Flask app<br/>(app.main:app)"]
    flask --> redis
    flask -->|HTTPS| solr["IATI.cloud Solr"]
```
