def merge_data(latest_data_full, new_data):
    """Merges new_data into latest_data_full"""
    if not latest_data_full:
        return new_data
        
    store_map = {d['name']: d for d in latest_data_full}
    for d in new_data:
        store_map[d['name']] = d
        
    return list(store_map.values())

print(merge_data([{"name":"A", "men":1}], [{"name":"A", "men":2}, {"name":"B", "men":1}]))
