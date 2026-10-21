import logger
client = logger.get_client()
print(client.auth.get_access_token().access_token)
