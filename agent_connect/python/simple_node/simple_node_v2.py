# AgentConnect: https://github.com/agent-network-protocol/AgentConnect
# Author: GaoWei Chang
# Email: chgaowei@gmail.com
# Website: https://agent-network-protocol.com/
#
# This project is open-sourced under the MIT License. For details, please see the LICENSE file.


# Simple DID Node
# Features:
# As a simple node, it can generate DID documents without third-party services, and provide HTTP and WebSocket services, 
# implementing a website's DID node capable of offering basic DID services:
# 1. Generate DID document
# 2. Provide HTTP server for clients to retrieve DID document
# 3. Provide WebSocket server for communication with other DIDs
# 4. Capable of initiating communication with other DIDs

# TODO
# 1. Session exception handling: active closure, client-initiated heartbeat
# 2. 

from typing import Dict, Optional, Tuple
import aiohttp
from fastapi import FastAPI, HTTPException, Response, WebSocket
import uvicorn
import websockets
import asyncio
import json
import logging
import traceback
from typing import Callable, Awaitable, Optional

from agent_connect.python.simple_node.simple_node_session import SimpleNodeSession
from agent_connect.python.simple_node.simple_wss_wraper import SimpleClientWssWraper, SimpleServerWssWraper, HeartbeatTimeoutError
from agent_connect.python.utils.crypto_tool import  get_pem_from_private_key
from agent_connect.python.utils.did_generate import did_generate


class SimpleNode:
    def __init__(self, 
                 host_domain: str, 
                 new_session_callback: Optional[Callable[[SimpleNodeSession], Awaitable[None]]],
                 host_port: str = "", 
                 host_ws_path: str = "/ws",  # Default WebSocket path is /ws
                 private_key_pem: Optional[str] = None, 
                 did: Optional[str] = None, 
                 did_document_json: Optional[str] = None,
                 ssl_cert_path: Optional[str] = None,
                 ssl_key_path: Optional[str] = None):
        '''

        '''
        self.host_domain = host_domain
        self.ws_new_session_callback = new_session_callback
        self.host_port = host_port
        self.host_ws_path = host_ws_path.strip()
        if not self.host_ws_path.startswith('/'):
            self.host_ws_path = '/' + self.host_ws_path

        if ssl_cert_path and ssl_key_path:
            base_url = f"wss://{host_domain}"
        else:
            base_url = f"ws://{host_domain}"
            
        if host_port:
            base_url += f":{host_port}"
        self.communication_wss_endpoint = f"{base_url}{self.host_ws_path}"

        self.private_key_pem = private_key_pem
        self.did = did
        self.did_document_json = did_document_json
        self.ssl_cert_path = ssl_cert_path
        self.ssl_key_path = ssl_key_path
        
        self.app = FastAPI()
        self.server_task = None  # For storing server task
        self._setup_fastapi()
    
    def _setup_fastapi(self):
        """
        Set up FastAPI routes and WebSocket endpoint.
        """
        # Set up routes
        self.app.get("/v1/did/{did}")(self._get_did_document_by_did)

        # Set up WebSocket route
        logging.info(f"Setting up WebSocket endpoint at path: {self.host_ws_path}")
        self.app.websocket(self.host_ws_path)(self._new_wss_server_session)

    def run(self):
        """
        Create a new coroutine to run the server.
        """
        loop = asyncio.get_event_loop()
        self.server_task = loop.create_task(self._run())

    async def _run(self):
        """
        Asynchronously run FastAPI server.
        """
        #  TODO: Adjust the port
        config = uvicorn.Config(
            app=self.app,
            host="0.0.0.0",
            port=int(self.host_port) if self.host_port else 8000,
            ssl_keyfile=self.ssl_key_path,
            ssl_certfile=self.ssl_cert_path
        )
        server = uvicorn.Server(config)
        await server.serve()

    def generate_did_document(self) -> Tuple[str, str, str]:
        """
        Generate DID document
        Note: This method does not automatically call set_did_info.
        Note: private_key_pem is a very important value, please keep it safe.

        Returns:
            Tuple[str, str, str]: A tuple containing private key in PEM format, DID string, and DID document JSON string.
        """
        private_key, _, did, did_document_json = did_generate(self.communication_wss_endpoint,
                                                              did_server_domain=self.host_domain,
                                                              did_server_port=self.host_port)

        # Convert private key to PEM format
        private_key_pem = get_pem_from_private_key(private_key)

        return private_key_pem, did, did_document_json

    def set_did_info(self, private_key_pem: str, did: str, did_document_json: str):
        """
        Set private key PEM, DID, and DID document JSON.

        Args:
            private_key_pem (str): Private key in PEM format.
            did (str): DID string.
            did_document_json (str): DID document in JSON format.
        """
        self.private_key_pem = private_key_pem
        self.did = did
        self.did_document_json = did_document_json

    def get_did_info(self) -> Tuple[str, str, str]:
        """
        Get private key PEM, DID, and DID document JSON.

        Returns:
            Tuple[str, str, str]: A tuple containing private key PEM, DID, and DID document JSON.
        """
        return self.private_key_pem, self.did, self.did_document_json

    async def _get_did_document_by_did(self, did: str):
        """
        FastAPI route to get DID document by DID.

        Args:
            did (str): DID to query.

        Returns:
            Response: DID document if found, otherwise raises HTTPException.
        """
        if self.did == did and self.did_document_json:
            return Response(content=self.did_document_json, media_type="application/text")
        else:
            raise HTTPException(status_code=404, detail="DID document not found")
        
    async def _fetch_did_document(self, did: str) -> Optional[str]:
        """
        Internal method to send HTTP request to fetch DID document.

        Args:
            did (str): DID to query.

        Returns:
            Optional[str]: If successful, returns the JSON string of the DID document; if failed, returns None.
        """
        try:
            # Extract domain and port from DID
            parts = did.split('@')
            if len(parts) != 2:
                logging.error(f"Invalid DID format: {did}")
                return None
            
            domain_port = parts[1].split(':')
            domain = domain_port[0]
            port = domain_port[1] if len(domain_port) > 1 else "80"  # Default to HTTP port 80
            
            # Construct URL
            url = f"http://{domain}:{port}/v1/did/{did}"
            
            # Send HTTP request
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        return await response.text()
                    else:
                        logging.error(f"Failed to fetch DID document. Status: {response.status}")
                        return None
        except Exception as e:
            logging.error(f"Error fetching DID document: {e}")
            return None

    async def _get_wss_address_from_did_document(self, did_document_json: str) -> Optional[str]:
        """
        Get WSS address from DID document.

        Args:
            did_document_json (str): JSON string of DID document.

        Returns:
            Optional[str]: If successful, returns the WSS address; if failed, returns None.
        """
        try:
            did_document = json.loads(did_document_json)
            
            # Find service of messageService type
            message_service = next((service for service in did_document.get('service', []) 
                                    if service.get('type') == 'messageService'), None)  
            
            if message_service:
                return message_service.get('serviceEndpoint', '')
            else:
                logging.error(f"messageService not found: {did_document}")
                return None
        except Exception as e:
            logging.error(f"Error fetching WSS address from DID document: {e}")
            return None

    async def _new_wss_server_session(self, websocket: WebSocket):
        """
        Handle new WebSocket server session.

        Args:
            websocket (WebSocket): The WebSocket connection.
        """
        await websocket.accept()
        simple_wss_wraper = SimpleServerWssWraper(websocket)

        # Create a new session
        simple_session = SimpleNodeSession(self.did, self.private_key_pem, self.did_document_json, simple_wss_wraper)
        
        # Wait for the other party to complete short-term key negotiation. Close the session if timeout
        success, remote_did, secret_info_json = await simple_session.wait_generate_short_term_key_passive()
        if not success:
            await websocket.close()
            return
        
        simple_session.set_remote_did(remote_did)
        try:
            await self.ws_new_session_callback(simple_session)
        except HeartbeatTimeoutError:
            logging.warning(f"DID:{remote_did} heartbeat timeout, closing the session")
        except ConnectionError as e:
            logging.error(f"Connection error: {e}")
        except Exception as e:
            # Log the exception stack trace
            error_stack = traceback.format_exc()
            logging.error(f"Error receiving message:\n{error_stack}")
        finally:
            # Close the session
            logging.info(f"Closing session with DID: {remote_did}")
            await simple_session.close()

    async def connect_to_did(self, destination_did: str, protocol_hash: Optional[str] = None) -> SimpleNodeSession:
        """
        Create a session with the target DID.

        Args:
            destination_did (str): The target DID to create a session with.

        Returns:
            SimpleNodeSession: The created session if successful, None otherwise.
        """
        # Query DID document based on DID
        did_document_json = await self._fetch_did_document(destination_did)
        if not did_document_json:
            logging.error(f"Unable to get DID document: {destination_did}")
            return None
        
        logging.info(f"DID [{destination_did}] document: {did_document_json}")  

        # Query corresponding WSS address based on DID document
        wss_address = await self._get_wss_address_from_did_document(did_document_json)
        if not wss_address:
            logging.error(f"No valid WSS address found: {destination_did}")
            return None
        
        logging.info(f"Found WSS address for target DID: {wss_address}")

        # Establish WSS connection, create session, save session
        try:
            websocket = await websockets.connect(wss_address)
            logging.info(f"Successfully connected to target DID's WSS address: {wss_address}")
        except Exception as e:
            logging.error(f"Failed to connect to target DID's WSS address: {e}")
            return None

        # Create SimpleClientWssWraper
        simple_wss_wraper = SimpleClientWssWraper(websocket)

        # Create SimpleNodeSession
        simple_session = SimpleNodeSession(self.did, 
                                           self.private_key_pem, 
                                           self.did_document_json, 
                                           simple_wss_wraper,
                                           protocol_hash=protocol_hash)

        # Wait for short-term key negotiation to complete
        success, remote_did, secret_info_json = await simple_session.generate_short_term_key_active(destination_did)
        if success:
            logging.info(f"Successfully established session with target DID {destination_did}")
            simple_session.set_remote_did(remote_did)
            return simple_session
        else:
            # Close the session
            await websocket.close()
            logging.error(f"Failed to establish session with target DID {destination_did}")
            return None

    async def stop(self):
        """
        Stop the server and close all sessions.
        """
        if self.server_task:
            self.server_task.cancel()
            try:
                await self.server_task
            except asyncio.CancelledError:
                pass



    

# Usage example:
# node = SimpleNode("localhost", 8000, "wss://example.com/ws")
# node.generate_did_document()
# node.run()

