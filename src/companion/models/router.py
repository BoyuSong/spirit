class ModelRouter:
    def select_role(self, task_type: str) -> str:
        return {"chat": "primary", "coding": "coding"}.get(task_type, "primary")
