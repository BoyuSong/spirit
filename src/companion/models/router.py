class ModelRouter:
    def select_role(self, task_type: str) -> str:
        return {
            "chat": "primary", "coordinator": "primary", "coding": "coding",
            "review": "review", "research": "research",
        }.get(task_type, "primary")
