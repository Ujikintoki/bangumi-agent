# Meta Data Collection
## GET 200
### /p1/characters/{characterID} 获取角色
``` json
{
  "collects": 0,
  "comment": 0,
  "id": 0,
  "name": "string",
  "nameCN": "string",
  "nsfw": true,
  "role": 1,
  "summary": "string"
}
```
### /p1/persons/{personID} 获取人物
## person
```json
{
  "career": "producer",
  "collects": 0,
  "comment": 0,
  "id": 0,
  "name": "string",
  "nameCN": "string",
  "nsfw": true,
  "summary": "string",
  "type": 1
}
```
### /p1/subjects/{subjectID}
```json
{
  "airtime": {
    "date": "2008-04-06",
    "month": 4,
    "weekday": 7,
    "year": 2008
  },
  "eps": 25,
  "id": 8,
  "metaTags": [],
  "name": "コードギアス 反逆のルルーシュR2",
  "nameCN": "Code Geass 反叛的鲁路修R2",
  "nsfw": false,
  "platform": {
    "alias": "tv",
    "enableHeader": true,
    "id": 1,
    "order": 0,
    "type": "TV",
    "typeCN": "TV",
    "wikiTpl": "TVAnime"
  },
  "rating": {
    "count": [
      44,
      15,
      32,
      66,
      145,
      457,
      1472,
      3190,
      2640,
      1377
    ],
    "score": 8.19,
    "total": 9438
  },
  "summary": "string",
  "type": 2,
}
```

### 404/500
```json
{
  "code": "string",
  "error": "string",
  "message": "string",
  "statusCode": 0
}
```
