# trafficgen — TalkChart Traffic Generator

app_id: trafficgen hybrid real+bot factory_pipeline
API: 0.0.0.0:8000 GET /watchtower/* canonical envelope Bearer WATCHTOWER_READ_TOKEN rotation hmac.compare_digest
Storage: SQLite WAL UNIQUE event_id identity retention 30d
Cursor: base64 cursor:<lastId> replay 1200 deterministic invalid 400 invalid_cursor
Events 17 implemented: SessionStarted PageView Click CTAClicked SessionEnded DataGapDetected DataGapHealed RateLimited CampaignCreated Started Stopped Updated SourceConnected Disconnected HealthChanged PageAssigned Removed
Unavailable 12: LandingReached SessionAbandoned NavigationCompleted DeliveryFailed RetryScheduled TrafficError ExporterHealth BotFlagged AnomalyDetected AbuseBlocked ConfigUpdated EmergencyPause
Analytics: p50 p95 byCampaign bySource byPage bot/real separate synthetic exclusion factory_pipeline->bot
ENV: TRAFFICGEN_API_BASE_URL http://127.0.0.1:8000 WATCHTOWER_READ_TOKEN openssl rand -hex 32
