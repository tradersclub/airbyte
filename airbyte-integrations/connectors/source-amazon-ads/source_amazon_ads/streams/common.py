#
# MIT License
#
# Copyright (c) 2020 Airbyte
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#

import json
from abc import ABC, abstractmethod
from http import HTTPStatus
from typing import Any, Iterable, Mapping, MutableMapping, Optional

import requests
from airbyte_cdk.sources.streams.core import Stream
from airbyte_cdk.sources.streams.http import HttpStream
from pydantic import BaseModel, ValidationError
from source_amazon_ads.common import PageToken, SourceContext
from source_amazon_ads.schemas import JSModel

URL_BASE = "https://advertising-api.amazon.com/"


class ErrorResponse(BaseModel):
    code: str
    details: str
    requestId: str


class BasicAmazonAdsStream(Stream, ABC):
    def __init__(self, config, context: SourceContext = None):
        self._ctx = context or SourceContext()
        self._config = config
        self._url = self._config.host or URL_BASE

    @property
    @abstractmethod
    def model(self) -> JSModel:
        """
        Pydantic model to represent json schema
        """

    def get_json_schema(self):
        return self.model.schema()


# Basic full refresh stream
class AmazonAdsStream(HttpStream, BasicAmazonAdsStream):
    def __init__(self, config, *args, context: SourceContext = None, **kwargs):
        BasicAmazonAdsStream.__init__(self, config, context=context or SourceContext())
        HttpStream.__init__(self, *args, **kwargs)

    @property
    def url_base(self):
        return self._url

    def next_page_token(self, response: requests.Response) -> Optional[Mapping[str, Any]]:
        return None

    def request_headers(self, *args, **kvargs) -> MutableMapping[str, Any]:
        return {"Amazon-Advertising-API-ClientId": self._config.client_id}

    def parse_response(self, response: requests.Response, **kwargs) -> Iterable[Mapping]:
        """
        :return an object representing single record in the response
        """
        yield from json.loads(response.text) if response else []

    def _send_request(self, request: requests.PreparedRequest, request_kwargs: Mapping[str, Any]) -> requests.Response:
        try:
            return super()._send_request(request, request_kwargs)
        except requests.exceptions.HTTPError as http_exception:
            response = http_exception.response
            if response.status_code in [HTTPStatus.FORBIDDEN]:
                try:
                    resp = ErrorResponse.parse_raw(response.text)
                    self.logger.warn(
                        f"Unexpected error {resp.code} when processing request {request.url} for {request.headers['Amazon-Advertising-API-Scope']} profile: {resp.details}"
                    )
                except ValidationError:
                    raise http_exception
            else:
                raise http_exception


class ContextStream(AmazonAdsStream):
    """
    Stream for getting resources based on context set by previous stream.
    """

    flattern_properties = []

    def parse_response(self, response: requests.Response, **kwargs) -> Iterable[Mapping]:
        for record in super().parse_response(response, **kwargs):
            for prop in self.flattern_properties:
                if prop in record:
                    record[prop] = json.dumps(record[prop])
            yield record

    def read_records(self, *args, **kvargs) -> Iterable[Mapping[str, Any]]:
        for profile in self._ctx.profiles:
            self._ctx.current_profile_id = profile.profileId
            yield from super().read_records(*args, **kvargs)

    def request_headers(self, *args, **kvargs) -> MutableMapping[str, Any]:
        headers = super().request_headers(*args, **kvargs)
        headers["Amazon-Advertising-API-Scope"] = str(self._ctx.current_profile_id)
        return headers


class PaginationStream(ContextStream):
    """
    Stream for getting resources with pagination support
    """

    page_size = 100

    def next_page_token(self, response: requests.Response) -> Optional[PageToken]:
        if not response:
            return None
        responses = json.loads(response.text)
        if len(responses) < self.page_size:
            self._ctx.current_token = PageToken()
            return None
        else:
            next_token = PageToken(self._ctx.current_token.offset + self.page_size)
            self._ctx.current_token = next_token
            return next_token

    def request_params(
        self,
        stream_state: Mapping[str, Any],
        stream_slice: Mapping[str, Any] = None,
        next_page_token: PageToken = None,
    ) -> MutableMapping[str, Any]:
        return {
            "startIndex": next_page_token.offset if next_page_token else 0,
            "count": self.page_size,
        }