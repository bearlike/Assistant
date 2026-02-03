#!/usr/bin/env python3
"""Meeseeks API.

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

from dotenv import load_dotenv

# Third-party modules
from flask import Flask, request
from flask_restx import Api, Resource, fields

# Adding the parent directory to the path before importing the custom modules
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

# Custom imports - Meeseeks core modules
if True:
    from core.classes import TaskQueue
    from core.common import get_logger
    from core.permissions import auto_approve
    from core.session_store import SessionStore
    from core.task_master import orchestrate_session

# Load environment variables
load_dotenv()
# Get the API token from the environment variables
# The default API token is "msk-strong-password"
MASTER_API_TOKEN = os.getenv("MASTER_API_TOKEN", "msk-strong-password")

# Initialize logger
logging = get_logger(name="meeseeks-api")
# logging.basicConfig(level=logging.DEBUG)
logging.info("Starting Meeseeks API server.")
logging.debug("Starting API server with API token: {}", MASTER_API_TOKEN)


# Create Flask application
app = Flask(__name__)
session_store = SessionStore()

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
    'session_id': fields.String(
        required=False, description='Session identifier for transcript storage'),
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
    """Log request metadata for debugging."""
    logging.debug("Endpoint: {}", request.endpoint)
    logging.debug("Headers: {}", request.headers)
    logging.debug("Body: {}", request.get_data())


@ns.route('/query')
class MeeseeksQuery(Resource):
    """Handle user queries and return executed action plans."""

    @api.doc(security='apikey')
    @api.expect(api.model('Query', {
        'query': fields.String(required=True, description='The user query'),
        'session_id': fields.String(required=False, description='Existing session id'),
        'session_tag': fields.String(required=False, description='Human-friendly tag'),
        'fork_from': fields.String(required=False, description='Session id or tag to fork'),
    }))
    @api.response(200, 'Success', task_queue_model)
    @api.response(400, 'Invalid input')
    @api.response(401, 'Unauthorized')
    def post(self) -> tuple[dict, int]:
        """Process a user query and return the action plan.

        Returns:
            Tuple of response payload and HTTP status code.
        """
        # Get API token from headers
        api_token = request.headers.get('X-API-Key', None)

        # Validate API token
        if api_token is None:
            return {"message": "API token is not provided."}, 401
        if api_token != MASTER_API_TOKEN:
            logging.warning(
                "Unauthorized API call attempt with token: {}", api_token)
            return {"message": "Unauthorized"}, 401

        # Get user query from request data
        request_data = request.get_json(silent=True) or {}
        user_query = request_data.get('query')
        if not user_query:
            return {"message": "Invalid input: 'query' is required"}, 400
        session_id = request_data.get('session_id')
        session_tag = request_data.get('session_tag')
        fork_from = request_data.get('fork_from')

        if fork_from:
            source_session_id = session_store.resolve_tag(fork_from) or fork_from
            session_id = session_store.fork_session(source_session_id)
        if session_tag and not session_id:
            resolved = session_store.resolve_tag(session_tag)
            session_id = resolved if resolved else None
        if not session_id:
            session_id = session_store.create_session()
        if session_tag:
            session_store.tag_session(session_id, session_tag)

        logging.info("Received user query: {}", user_query)

        # Generate action plan from user query
        task_queue: TaskQueue = orchestrate_session(
            user_query=user_query,
            session_id=session_id,
            session_store=session_store,
            approval_callback=auto_approve,
        )
        # Deep copy the variable into another variable
        task_result = deepcopy(task_queue.task_result)
        to_return = task_queue.dict()
        to_return["task_result"] = task_result
        # Return TaskQueue as JSON
        logging.info("Returning executed action plan.")
        to_return["session_id"] = session_id
        return to_return, 200


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5123)
