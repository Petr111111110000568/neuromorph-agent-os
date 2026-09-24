from pathlib import Path
import yaml

class SkillLoader:
    def __init__(self, root="srf/skills"):
        self.root = Path(root)

    def discover(self):
        result = []
        for path in sorted(self.root.glob("*/SKILL.md")):
            meta, body = self._frontmatter(path.read_text(encoding="utf-8"))
            result.append({
                "id": meta.get("name", path.parent.name),
                "description": meta.get("description", ""),
                "path": str(path),
                "instructions": body,
            })
        return result

    def _frontmatter(self, text):
        if not text.startswith("---"):
            return {}, text
        parts = text.split("---", 2)
        if len(parts) != 3:
            return {}, text
        return yaml.safe_load(parts[1]) or {}, parts[2].lstrip()

    def load(self, skill_id):
        for skill in self.discover():
            if skill["id"] == skill_id:
                return skill
        raise KeyError(skill_id)
