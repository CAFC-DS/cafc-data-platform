"""
Test script for Impect API utilities
"""
from impect_api import (
    get_iterations,
    get_matches,
    get_squads,
    get_players,
    get_coaches,
    get_stadiums,
    get_countries
)

def test_api():
    """Test all API endpoints"""
    print("=" * 60)
    print("Testing Impect API Utilities")
    print("=" * 60)

    try:
        print("\n1. Testing get_iterations()...")
        iterations = get_iterations()
        print(f"   Success! Retrieved {len(iterations) if isinstance(iterations, list) else 'data'}")
        print(f"   Sample: {str(iterations)[:200]}...")

    except Exception as e:
        print(f"   Error: {str(e)}")

    try:
        print("\n2. Testing get_matches(iteration_id=1410)...")
        matches = get_matches(iteration_id=1410)
        print(f"   Success! Retrieved {len(matches) if isinstance(matches, list) else 'data'}")
        print(f"   Sample: {str(matches)[:200]}...")

    except Exception as e:
        print(f"   Error: {str(e)}")

    try:
        print("\n3. Testing get_squads(iteration_id=1410)...")
        squads = get_squads(iteration_id=1410)
        print(f"   Success! Retrieved {len(squads) if isinstance(squads, list) else 'data'}")
        print(f"   Sample: {str(squads)[:200]}...")

    except Exception as e:
        print(f"   Error: {str(e)}")

    try:
        print("\n4. Testing get_players(iteration_id=1410)...")
        players = get_players(iteration_id=1410)
        print(f"   Success! Retrieved {len(players) if isinstance(players, list) else 'data'}")
        print(f"   Sample: {str(players)[:200]}...")

    except Exception as e:
        print(f"   Error: {str(e)}")

    try:
        print("\n5. Testing get_coaches(iteration_id=1410)...")
        coaches = get_coaches(iteration_id=1410)
        print(f"   Success! Retrieved {len(coaches) if isinstance(coaches, list) else 'data'}")
        print(f"   Sample: {str(coaches)[:200]}...")

    except Exception as e:
        print(f"   Error: {str(e)}")

    try:
        print("\n6. Testing get_stadiums(iteration_id=1410)...")
        stadiums = get_stadiums(iteration_id=1410)
        print(f"   Success! Retrieved {len(stadiums) if isinstance(stadiums, list) else 'data'}")
        print(f"   Sample: {str(stadiums)[:200]}...")

    except Exception as e:
        print(f"   Error: {str(e)}")

    try:
        print("\n7. Testing get_countries()...")
        countries = get_countries()
        print(f"   Success! Retrieved {len(countries) if isinstance(countries, list) else 'data'}")
        print(f"   Sample: {str(countries)[:200]}...")

    except Exception as e:
        print(f"   Error: {str(e)}")

    print("\n" + "=" * 60)
    print("Test completed!")
    print("=" * 60)

if __name__ == "__main__":
    test_api()
