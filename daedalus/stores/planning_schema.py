"""Seed finite planning limits for projects created before budgeted plans."""

MIGRATION = """
INSERT OR IGNORE INTO planning_budgets(project_id,goal_contract_revision,max_depth,max_tasks,max_tokens)
SELECT p.id,p.goal_revision,3,
       CASE WHEN (SELECT count(*) FROM board_tasks b WHERE b.project_id = p.id) > 50
            THEN (SELECT count(*) FROM board_tasks b WHERE b.project_id = p.id) ELSE 50 END,
       100000 FROM projects p;
"""


__all__ = ["MIGRATION"]
