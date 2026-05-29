import json

import requests


class BangumiToolbox:
    def __init__(self):
        # 严格遵守 Bangumi 开发者规范的 User-Agent
        self.headers = {
            "User-Agent": "Akina7/bangumi-agent-dev (Private Agent Project)",
            "Accept": "application/json",
        }
        self.base_url = "https://api.bgm.tv"
        # 使用 Session 保持连接池，提升后续多次请求的速度
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def get_subject_info(self, subject_id: int):
        """
        Tool 1: 根据条目 ID 获取番剧的详细信息
        注意：Agent 之后会将原始 JSON 丢给大模型，为了节省 Token 和防止幻觉，
        我们必须在这里做【数据清洗】，只返回 LLM 需要的关键字段。
        """
        url = f"{self.base_url}/v0/subjects/{subject_id}"
        print(f"正在请求: {url}")

        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()  # 检查 HTTP 错误
            data = response.json()

            # 【关键步骤：数据清洗】只提取对 Agent 推荐有用的字段
            cleaned_data = {
                "id": data.get("id"),
                "name": data.get("name"),
                "name_cn": data.get("name_cn"),
                "summary": data.get("summary"),
                "score": data.get("rating", {}).get("score", "暂无评分"),
                "tags": [
                    tag["name"] for tag in data.get("tags", [])[:5]
                ],  # 只取前5个核心标签
            }
            return cleaned_data

        except requests.exceptions.RequestException as e:
            return {"error": f"API 请求失败: {str(e)}"}


# ================= 测试代码 =================
if __name__ == "__main__":
    toolbox = BangumiToolbox()

    # 测试获取《终将成为你》 (subject_id: 240038) 的信息
    print("--- 正在测试 Bangumi API ---")
    result = toolbox.get_subject_info(240038)

    print("\n✅ 清洗后的返回结果 (将输入给大模型的数据):")
    print(json.dumps(result, ensure_ascii=False, indent=2))
