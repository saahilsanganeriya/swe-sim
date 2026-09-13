"""Pipeline stages. Each stage is a plain function over (Workspace, ScenarioSpec, RunConfig) that the CLI and
the Temporal workflows call; stages never import the CLI."""
