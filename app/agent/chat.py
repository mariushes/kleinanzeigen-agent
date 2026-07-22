"""Agentic chat to query and reason on the data related to listings, vehicles and knowledge base.
"""

from langchain.agents import create_agent
from app.agent.tools import (
    get_criteria_profiles,
    get_full_listing,
    get_listing_criteria_assessment,
    get_listing_knowledge,
    get_listings,
)
from langchain_community.agent_toolkits.sql.toolkit import SQLDatabaseToolkit
from langchain_google_genai import ChatGoogleGenerativeAI
from app.db.session import get_langchain_db_wrapper

model = ChatGoogleGenerativeAI(
    model="gemini-3.5-flash",
    temperature=1.0,
    max_tokens=None,
    timeout=None,
    max_retries=2,
)
db = get_langchain_db_wrapper()
toolkit = SQLDatabaseToolkit(db=db, llm=model)
tools = toolkit.get_tools() + [
    get_listings,
    get_full_listing,
    get_listing_knowledge,
    get_criteria_profiles,
    get_listing_criteria_assessment,
]

agent = create_agent(model=model, tools=tools)

agent.run("Get me all listings.")