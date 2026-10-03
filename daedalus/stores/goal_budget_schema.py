"""Keep an activated project money boundary across later goal revisions."""

MIGRATION = """
CREATE TABLE project_goal_budgets (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE RESTRICT,
    budget_id TEXT NOT NULL UNIQUE,
    limit_microusd INTEGER NOT NULL CHECK(limit_microusd >= 0),
    coordination_limit_microusd INTEGER NOT NULL CHECK(coordination_limit_microusd >= 0 AND coordination_limit_microusd <= limit_microusd),
    activated_goal_revision INTEGER NOT NULL CHECK(activated_goal_revision >= 1),
    activated_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE goal_budget_admissions (
    reservation_id TEXT PRIMARY KEY REFERENCES inference_reservations(id) DEFERRABLE INITIALLY DEFERRED,
    budget_id TEXT NOT NULL REFERENCES project_goal_budgets(budget_id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    goal_revision INTEGER NOT NULL,
    charge_class TEXT NOT NULL CHECK(charge_class IN ('coordination','work')),
    root_session_id TEXT NOT NULL,
    attributed_at TEXT NOT NULL,
    FOREIGN KEY(project_id,goal_revision) REFERENCES project_goal_revisions(project_id,goal_revision)
);
CREATE TABLE goal_budget_allocations (
    slot_id TEXT PRIMARY KEY REFERENCES comparison_funding_slots(id) DEFERRABLE INITIALLY DEFERRED,
    budget_id TEXT NOT NULL REFERENCES project_goal_budgets(budget_id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    goal_revision INTEGER NOT NULL,
    charge_class TEXT NOT NULL DEFAULT 'work' CHECK(charge_class = 'work'),
    attributed_at TEXT NOT NULL,
    FOREIGN KEY(project_id,goal_revision) REFERENCES project_goal_revisions(project_id,goal_revision)
);
CREATE TRIGGER goal_budget_admissions_no_update BEFORE UPDATE ON goal_budget_admissions
BEGIN SELECT RAISE(ABORT,'goal budget admission attribution is immutable'); END;
CREATE TRIGGER goal_budget_admissions_no_delete BEFORE DELETE ON goal_budget_admissions
BEGIN SELECT RAISE(ABORT,'goal budget admission attribution is retained'); END;
CREATE TRIGGER goal_budget_allocations_no_update BEFORE UPDATE ON goal_budget_allocations
BEGIN SELECT RAISE(ABORT,'goal budget allocation attribution is immutable'); END;
CREATE TRIGGER goal_budget_allocations_no_delete BEFORE DELETE ON goal_budget_allocations
BEGIN SELECT RAISE(ABORT,'goal budget allocation attribution is retained'); END;
CREATE TRIGGER project_goal_budget_origin_immutable
BEFORE UPDATE OF project_id,budget_id,activated_goal_revision,activated_at ON project_goal_budgets
BEGIN SELECT RAISE(ABORT,'the budget accounting origin is immutable'); END;
CREATE TRIGGER project_goal_budget_no_delete BEFORE DELETE ON project_goal_budgets
BEGIN SELECT RAISE(ABORT,'a budget with attributed usage cannot be deleted'); END;
"""
