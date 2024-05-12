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
from typing import Dict

# Third-party modules
from flask import Flask, request, jsonify
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

# Initialize logger
logging = get_logger(name="meeseeks-api")

# Create Flask application
app = Flask(__name__)
# Create API instance with Swagger documentation
api = Api(app, version='1.0', title='Meeseeks API',
          description='Interact with Meeseeks through a REST API',
          doc='/swagger-ui/')

# Define API namespace
ns = api.namespace('api', description='Meeseeks operations')

# Define API model for request and response
task_queue_model = api.model('TaskQueue', {
    'human_message': fields.String(
        required=True, description='The original user query'),
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


@ns.route('/query')
class MeeseeksQuery(Resource):
    """
    Endpoint to submit a query to Meeseeks and receive the executed
    action plan as a JSON response.
    """

    @api.doc(security='apiKey')
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
        api_token = request.headers.get('X-API-Key')

        # Validate API token
        if api_token != os.getenv("MASTER_API_TOKEN"):
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

        # Return TaskQueue as JSON
        logging.info("Returning executed action plan.")
        return task_queue.dict(), 200


if __name__ == '__main__':
    app.run(debug=True)
