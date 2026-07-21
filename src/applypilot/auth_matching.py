"""Bounded, fail-closed unique matching for email authentication requests."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import TypeVar

Request = TypeVar("Request")
Message = TypeVar("Message")
RequestId = TypeVar("RequestId")
MessageId = TypeVar("MessageId")


def unique_assignments(
    requests: Sequence[Request],
    messages: Sequence[Message],
    *,
    request_id: Callable[[Request], RequestId],
    message_id: Callable[[Message], MessageId],
    eligible: Callable[[Request, Message], bool],
    max_items: int = 1000,
) -> list[tuple[Request, Message]]:
    """Return assignments only for components with one complete perfect matching."""
    bounded_requests = list(requests[:max_items])
    request_by_id = {request_id(request): request for request in bounded_requests}
    message_by_id: dict[MessageId, Message] = {}
    for message in messages:
        message_by_id.setdefault(message_id(message), message)
        if len(message_by_id) == max_items:
            break

    request_edges: dict[RequestId, list[MessageId]] = {}
    message_edges: dict[MessageId, list[RequestId]] = {
        identifier: [] for identifier in message_by_id
    }
    for identifier, request in request_by_id.items():
        edges = []
        for candidate_id, message in message_by_id.items():
            if eligible(request, message):
                edges.append(candidate_id)
                message_edges[candidate_id].append(identifier)
        request_edges[identifier] = edges

    assigned: dict[RequestId, MessageId] = {}
    seen_requests: set[RequestId] = set()
    for start in request_by_id:
        if start in seen_requests or not request_edges[start]:
            continue
        component_requests: set[RequestId] = set()
        component_messages: set[MessageId] = set()
        queue = [start]
        while queue:
            identifier = queue.pop()
            if identifier in component_requests:
                continue
            component_requests.add(identifier)
            seen_requests.add(identifier)
            for candidate_id in request_edges[identifier]:
                if candidate_id in component_messages:
                    continue
                component_messages.add(candidate_id)
                queue.extend(message_edges[candidate_id])

        if len(component_requests) != len(component_messages):
            continue
        matching = perfect_component_matching(component_requests, request_edges)
        if matching is None or not matching_is_unique(matching, request_edges):
            continue
        assigned.update(matching)

    return [
        (request, message_by_id[assigned[request_id(request)]])
        for request in bounded_requests
        if request_id(request) in assigned
    ]


def perfect_component_matching(
    component_requests: Iterable[RequestId],
    request_edges: dict[RequestId, list[MessageId]],
) -> dict[RequestId, MessageId] | None:
    component_requests = tuple(component_requests)
    pair_request: dict[RequestId, MessageId | None] = {
        identifier: None for identifier in component_requests
    }
    pair_message: dict[MessageId, RequestId] = {}
    distance: dict[RequestId, int | None] = {}

    def find_augmenting_layers() -> bool:
        queue = []
        for identifier in component_requests:
            if pair_request[identifier] is None:
                distance[identifier] = 0
                queue.append(identifier)
            else:
                distance[identifier] = None
        found = False
        index = 0
        while index < len(queue):
            identifier = queue[index]
            index += 1
            for candidate_id in request_edges[identifier]:
                paired_request = pair_message.get(candidate_id)
                if paired_request is None:
                    found = True
                elif distance[paired_request] is None:
                    distance[paired_request] = distance[identifier] + 1
                    queue.append(paired_request)
        return found

    def augment(identifier: RequestId) -> bool:
        stack = [(identifier, 0)]
        path: list[tuple[RequestId, MessageId]] = []
        while stack:
            current, edge_index = stack[-1]
            edges = request_edges[current]
            if edge_index == len(edges):
                distance[current] = None
                stack.pop()
                if path and len(path) >= len(stack):
                    path.pop()
                continue
            candidate_id = edges[edge_index]
            stack[-1] = (current, edge_index + 1)
            paired_request = pair_message.get(candidate_id)
            if paired_request is None:
                pair_request[current] = candidate_id
                pair_message[candidate_id] = current
                for path_request, path_message in reversed(path):
                    pair_request[path_request] = path_message
                    pair_message[path_message] = path_request
                return True
            current_distance = distance[current]
            if current_distance is not None and distance[paired_request] == current_distance + 1:
                path.append((current, candidate_id))
                stack.append((paired_request, 0))
        return False

    matched = 0
    while find_augmenting_layers():
        for identifier in component_requests:
            if pair_request[identifier] is None and augment(identifier):
                matched += 1
    if matched != len(component_requests):
        return None
    return {identifier: candidate for identifier, candidate in pair_request.items() if candidate is not None}


def matching_is_unique(matching, request_edges) -> bool:
    matched_request_for_message = {
        candidate_id: request_id for request_id, candidate_id in matching.items()
    }
    alternating_edges = {request_id: set() for request_id in matching}
    indegree = {request_id: 0 for request_id in matching}
    for request_id in matching:
        for candidate_id in request_edges[request_id]:
            paired_request = matched_request_for_message[candidate_id]
            if paired_request == request_id:
                continue
            if paired_request not in alternating_edges[request_id]:
                alternating_edges[request_id].add(paired_request)
                indegree[paired_request] += 1

    queue = [request_id for request_id, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        request_id = queue.pop()
        visited += 1
        for paired_request in alternating_edges[request_id]:
            indegree[paired_request] -= 1
            if indegree[paired_request] == 0:
                queue.append(paired_request)
    return visited == len(matching)
