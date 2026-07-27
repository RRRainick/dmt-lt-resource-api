python3 ./api_test.py \
	  --ip 192.168.134.178 \
	    --port 8021 \
	      --node-id IPL238 \
	        --modality api-test-mode \
		--timeout 10

# ktime curl --noproxy '*' -v \
# k	  --connect-timeout 3 \
# k	    --max-time 15 \
# k	      'http://192.168.134.178:8021/resource/status?node_id=IPL238&request_id=1'
