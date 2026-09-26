# -*- coding: utf-8 -*-
"""فولدر الإيند بوينتات — كل ملف هنا فيه APIRouter لمجموعة إيند بوينتات.

لإضافة إيند بوينت جديد:
  1) اعمل ملف جديد هنا (مثلاً sales.py) وبيه router = APIRouter(...).
  2) ضيف اسمه للقائمة all_routers بالأسفل.
main.py يربط كل الراوترات بالقائمة تلقائياً، فما تحتاج تعدّله.
"""

# الشرح: نستورد راوتر كل ملف ونجمعهم بقائمة وحدة. main.py يلف على هذي
# القائمة ويسوي app.include_router لكل واحد — هيچ إضافة ملف جديد تحتاج
# سطرين هنا فقط.
from app.endpoints.agent import router as agent_router
from app.endpoints.assistant import router as assistant_router
from app.endpoints.chat import router as chat_router

all_routers = [
    chat_router,
    agent_router,
    assistant_router,
]
