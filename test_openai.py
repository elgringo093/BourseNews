import os
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

resp = client.responses.create(
    model="gpt-5.2",
    input="Dis bonjour et explique en une phrase ce que fait une API."
)

print(resp.output_text)
