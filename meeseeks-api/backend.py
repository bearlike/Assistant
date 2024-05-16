#!/usr/bin/env python3
"""
Meeseeks API

This module implements a REST API for Meeseeks using Flask-RESTX.
It provides a single endpoint to interact with the Meeseeks core,
allowing users to submit queries and receive the executed action plan
as a JSON response.
"""
# TODO: API key authentication and rate limiting not implemented yet.
# Standard library modules
import os
import sys
from copy import deepcopy
from typing import Dict

# Third-party modules
from flask import Flask, request
from flask_restx import Api, Resource, fields
from dotenv import load_dotenv

# Adding the parent directory to the path before importing the custom modules
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

# Custom imports - Meeseeks core modules
if True:
    from core.task_master import generate_action_plan, run_action_plan
    from core.classes import TaskQueue
    from core.common import get_logger

# Load environment variables
load_dotenv()
# Get the API token from the environment variables
# The default API token is "msk-strong-password"
MASTER_API_TOKEN = os.getenv("MASTER_API_TOKEN", "msk-strong-password")

# Initialize logger
logging = get_logger(name="meeseeks-api")
# logging.basicConfig(level=logging.DEBUG)
logging.info("Starting Meeseeks API server.")
logging.debug("Starting API server with API token: %s", MASTER_API_TOKEN)


# Create Flask application
app = Flask(__name__)

authorizations = {
    'apikey': {
        'type': 'apiKey',
        'in': 'header',
        'name': 'X-API-KEY'
    }
}
VERSION = os.getenv("VERSION", "(Dev)")
# Create API instance with Swagger documentation
api = Api(app, version=VERSION, title='Meeseeks API',
          description='Interact with Meeseeks through a REST API',
          doc='/swagger-ui/', authorizations=authorizations, security='apikey')

# Define API namespace
ns = api.namespace('api', description='Meeseeks operations')

# Define API model for request and response
task_queue_model = api.model('TaskQueue', {
    'human_message': fields.String(
        required=True, description='The original user query'),
    'task_result': fields.String(
        required=True, description='Combined response of all action steps'),
    'action_steps': fields.List(fields.Nested(api.model('ActionStep', {
        'action_consumer': fields.String(
            required=True,
            description='The tool responsible for executing the action'),
        'action_type': fields.String(
            required=True,
            description='The type of action to be performed (get/set)'),
        'action_argument': fields.String(
            required=True,
            description='The specific argument for the action'),
        'result': fields.String(
            description='The result of the executed action')
    }))),
})


@app.before_request
def log_request_info():
    logging.debug('Endpoint: %s', request.endpoint)
    logging.debug('Headers: %s', request.headers)
    logging.debug('Body: %s', request.get_data())


@ns.route('/query')
class MeeseeksQuery(Resource):
    """
    Endpoint to submit a query to Meeseeks and receive the executed
    action plan as a JSON response.
    """

    @api.doc(security='apikey')
    @api.expect(api.model('Query', {'query': fields.String(
        required=True, description='The user query')}))
    @api.response(200, 'Success', task_queue_model)
    @api.response(400, 'Invalid input')
    @api.response(401, 'Unauthorized')
    def post(self) -> Dict:
        """
        Process a user query, generate and execute the action plan,
        and return the result as a JSON.

        Requires a valid API token for authorization.
        """
        # Get API token from headers
        api_token = request.headers.get('X-API-Key', None)

        # Validate API token
        if api_token is None:
            return {"message": "API token is not provided."}, 401
        if api_token != MASTER_API_TOKEN:
            logging.warning(
                "Unauthorized API call attempt with token: %s", api_token)
            return {"message": "Unauthorized"}, 401

        # Get user query from request data
        user_query = request.json.get('query')
        if not user_query:
            return {"message": "Invalid input: 'query' is required"}, 400

        logging.info("Received user query: %s", user_query)

        # Generate action plan from user query
        task_queue: TaskQueue = generate_action_plan(user_query=user_query)

        # Execute action plan
        task_queue = run_action_plan(task_queue)
        # Deep copy the variable into another variable
        task_result = deepcopy(task_queue.task_result)
        to_return = task_queue.dict()
        to_return["task_result"] = task_result
        # Return TaskQueue as JSON
        logging.info("Returning executed action plan.")
        return to_return, 200


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5123)
